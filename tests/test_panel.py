"""
The panel's pages: signing in, reading processors, and the browser-specific
parts of security -- redirects instead of JSON, escaping, and the headers
that stop injected script from running.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from api.main import app
from core import auth, users
from doctypes import authoring, registry, seed

pytestmark = pytest.mark.usefixtures("fast_hashing")

PASSWORD = "correct horse battery"


@pytest.fixture(autouse=True)
def _seeded(session_factory):
    registry.clear_cache()
    with session_factory() as s:
        seed.sync(s, seed.load_seeds())
        users.create(s, "ana@example.com", PASSWORD, role="admin")
    yield
    registry.clear_cache()


@pytest.fixture
def browser(_wired):
    with TestClient(app, base_url="https://testserver") as c:
        yield c


@pytest.fixture
def signed_in(browser):
    response = browser.post(
        "/panel/login", data={"email": "ana@example.com", "password": PASSWORD}
    )
    assert response.status_code == 200
    return browser


class TestSigningIn:
    def test_a_signed_out_browser_is_sent_to_sign_in(self, browser):
        response = browser.get("/panel", follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == "/panel/login"

    def test_the_api_still_answers_json_not_redirects(self, browser):
        assert browser.get("/processors").status_code == 401

    def test_a_good_sign_in_lands_on_the_processors(self, browser):
        response = browser.post(
            "/panel/login",
            data={"email": "ana@example.com", "password": PASSWORD},
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert response.headers["location"] == "/panel"
        assert auth.SESSION_COOKIE in response.headers["set-cookie"]

    def test_a_bad_one_shows_the_form_again_keeping_the_email(self, browser):
        response = browser.post(
            "/panel/login", data={"email": "ana@example.com", "password": "not it at all"}
        )
        assert response.status_code == 401
        assert "Wrong email or password" in response.text
        assert 'value="ana@example.com"' in response.text

    def test_a_signed_in_browser_skips_the_form(self, signed_in):
        response = signed_in.get("/panel/login", follow_redirects=False)
        assert response.headers["location"] == "/panel"

    def test_signing_out_needs_the_pages_csrf_token(self, signed_in):
        assert signed_in.post("/panel/logout").status_code == 403

    def test_signing_out_with_it_redirects_the_htmx_way(self, signed_in, session_factory):
        token = _csrf(signed_in.get("/panel").text)
        response = signed_in.post("/panel/logout", headers={auth.CSRF_HEADER: token})
        assert response.headers["HX-Redirect"] == "/panel/login"
        assert signed_in.get("/panel", follow_redirects=False).status_code == 303

    def test_an_expired_htmx_request_is_redirected_not_left_blank(self, browser):
        response = browser.get("/panel", headers={"HX-Request": "true"})
        assert response.headers["HX-Redirect"] == "/panel/login"


class TestReadingProcessors:
    def test_the_list_shows_every_processor_and_its_state(self, signed_in):
        page = signed_in.get("/panel").text
        assert "invoice" in page
        assert "receipt" in page
        assert "active" in page

    def test_the_page_carries_the_csrf_token_for_htmx(self, signed_in):
        assert 'hx-headers=\'{"X-CSRF-Token": "' in signed_in.get("/panel").text

    def test_history_lists_versions_with_who_wrote_them(self, signed_in):
        page = signed_in.get("/panel/processors/invoice").text
        assert "v1" in page
        assert "seed" in page
        assert "live" in page

    def test_a_version_is_shown_as_seed_yaml_with_its_code(self, signed_in):
        page = signed_in.get("/panel/processors/invoice/versions/1").text
        assert "name: invoice" in page
        assert "code: |" in page
        assert "def transform(data):" in page

    def test_a_diff_shows_exactly_what_changed(self, signed_in, session_factory):
        with session_factory() as s:
            base = authoring.get_version(s, "invoice", 1).definition
            authoring.create_draft(
                s, {**base, "display_name": "Supplier invoice"}, based_on=1, created_by="t"
            )

        page = signed_in.get("/panel/processors/invoice/diff?a=1&b=2").text
        assert '<span class="del">-display_name: Invoice</span>' in page
        assert '<span class="add">+display_name: Supplier invoice</span>' in page

    def test_history_offers_the_diff_against_the_base(self, signed_in, session_factory):
        with session_factory() as s:
            base = authoring.get_version(s, "invoice", 1).definition
            authoring.create_draft(s, base, based_on=1, created_by="t")
        assert "diff?a=1&amp;b=2" in signed_in.get("/panel/processors/invoice").text

    def test_an_unknown_processor_is_a_page_not_json(self, signed_in):
        response = signed_in.get("/panel/processors/nothing")
        assert response.status_code == 404
        assert "text/html" in response.headers["content-type"]
        assert "no processor called" in response.text


class TestBrowserSecurity:
    def test_every_page_forbids_foreign_and_inline_script(self, browser):
        policy = browser.get("/panel/login").headers["content-security-policy"]
        assert "script-src 'self'" in policy
        assert "unsafe-inline" not in policy
        assert "frame-ancestors 'none'" in policy

    def test_the_api_does_not_get_page_headers(self, client):
        assert "content-security-policy" not in client.get("/processors").headers

    def test_what_authors_write_is_escaped(self, signed_in, session_factory):
        """A display name is author input, and author input is not markup."""
        with session_factory() as s:
            base = authoring.get_version(s, "invoice", 1).definition
            authoring.create_draft(
                s, {**base, "display_name": "<script>alert(1)</script>"},
                based_on=1, created_by="t",
            )
        page = signed_in.get("/panel").text
        assert "<script>alert(1)</script>" not in page
        assert "&lt;script&gt;" in page

    def test_htmx_is_served_from_here_not_a_cdn(self, browser):
        page = browser.get("/panel/login").text
        assert 'src="/panel/static/htmx-2.0.4.min.js"' in page
        assert browser.get("/panel/static/htmx-2.0.4.min.js").status_code == 200


def _csrf(page: str) -> str:
    marker = '"X-CSRF-Token": "'
    start = page.index(marker) + len(marker)
    return page[start : page.index('"', start)]
