"""
The panel's pages: signing in, reading processors, and the browser-specific
parts of security -- redirects instead of JSON, escaping, and the headers
that stop injected script from running.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from api.main import app
from core import auth, repository, users
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


# --- Editing and trying (1.4) -------------------------------------------------


@pytest.fixture
def htmx(signed_in):
    """Headers an HTMX request from a panel page carries."""
    token = _csrf(signed_in.get("/panel").text)
    return {auth.CSRF_HEADER: token, "HX-Request": "true"}


@pytest.fixture
def fake_model():
    from api.routes import processors as api_processors

    from .conftest import FakeExtractor

    extractor = FakeExtractor(
        {"subtotal": "100.00", "tax_amount": "20.00", "total": None, "vendor": "ACME"}
    )
    app.dependency_overrides[api_processors.get_extractor] = lambda: extractor
    yield extractor
    app.dependency_overrides.pop(api_processors.get_extractor, None)


def _draft(signed_in, htmx) -> str:
    response = signed_in.post("/panel/processors/invoice/versions/1/draft", headers=htmx)
    return response.headers["HX-Redirect"]


class TestEditing:
    def test_a_draft_starts_from_a_published_version(self, signed_in, htmx, session_factory):
        assert _draft(signed_in, htmx) == "/panel/processors/invoice/versions/2/edit"
        with session_factory() as s:
            row = authoring.get_version(s, "invoice", 2)
        assert (row.based_on_version, row.created_by) == (1, "user:ana@example.com")

    def test_the_editor_gives_the_code_its_own_box(self, signed_in, htmx):
        page = signed_in.get(_draft(signed_in, htmx)).text
        yaml_box = page.split('name="text"')[1].split("</textarea>")[0]
        code_box = page.split('name="code"')[1].split("</textarea>")[0]
        assert "def transform" not in yaml_box
        assert "examples:" in yaml_box
        assert "def transform(data):" in code_box

    def test_saving_keeps_yaml_and_code_together(self, signed_in, htmx, session_factory):
        url = _draft(signed_in, htmx)
        with session_factory() as s:
            text, code = authoring.editable(authoring.get_version(s, "invoice", 2).definition)

        response = signed_in.post(
            url.replace("/edit", "/save"),
            data={"text": text.replace("display_name: Invoice", "display_name: Edited"),
                  "code": code},
            headers=htmx,
        )
        assert response.status_code == 200
        assert "Saved invoice v2" in response.text
        with session_factory() as s:
            row = authoring.get_version(s, "invoice", 2)
        assert row.display_name == "Edited"
        assert "def transform" in row.definition["transform"]["code"]

    def test_an_uploaded_py_replaces_the_code_box(self, signed_in, htmx, session_factory):
        url = _draft(signed_in, htmx)
        with session_factory() as s:
            text, _ = authoring.editable(authoring.get_version(s, "invoice", 2).definition)
        uploaded = (
            "from decimal import Decimal\n\ndef transform(data):\n    return data\n"
        )
        # Examples would fail on publish, not here: saving a draft is allowed
        # to be work in progress.
        signed_in.post(
            url.replace("/edit", "/save"),
            data={"text": text, "code": "ignored"},
            files={"code_file": ("t.py", uploaded.encode(), "text/x-python")},
            headers=htmx,
        )
        with session_factory() as s:
            stored = authoring.get_version(s, "invoice", 2).definition
        assert stored["transform"]["code"] == uploaded

    def test_problems_come_back_beside_the_form_with_their_paths(self, signed_in, htmx):
        url = _draft(signed_in, htmx)
        response = signed_in.post(
            url.replace("/edit", "/save"),
            data={"text": "name: invoice\nmatches: {keywords: [x]}\nfields: {}", "code": ""},
            headers=htmx,
        )
        assert response.status_code == 422
        assert "not a valid processor" in response.text
        assert "<code>fields:" in response.text
        assert "<html" not in response.text  # a fragment, not a page

    def test_a_published_version_opens_read_only(self, signed_in):
        response = signed_in.get(
            "/panel/processors/invoice/versions/1/edit", follow_redirects=False
        )
        assert response.headers["location"] == "/panel/processors/invoice/versions/1"

    def test_a_new_processor_becomes_a_draft(self, signed_in, htmx):
        response = signed_in.post(
            "/panel/new",
            data={"text": "name: credit_note\nmatches: {keywords: [credit note]}\n"
                          "fields: {total: {type: money, description: The total.}}"},
            headers=htmx,
        )
        assert response.headers["HX-Redirect"] == "/panel/processors/credit_note/versions/1/edit"

    def test_the_skeleton_for_a_new_processor_is_itself_valid(self):
        from panel.routes import NEW_PROCESSOR

        authoring.parse(authoring.join_edit(NEW_PROCESSOR, ""))

    def test_an_author_may_edit_but_this_needs_a_session_token(self, signed_in):
        assert signed_in.post("/panel/processors/invoice/versions/1/draft").status_code == 403


class TestTrying:
    TEXT = "ACME LTD\nINVOICE INV-1\nDue Date: 1 May 2026\nSubtotal 100.00\nVAT 20.00"

    def test_a_trial_shows_routing_fields_and_what_was_derived(self, signed_in, htmx, fake_model):
        response = signed_in.post(
            "/panel/processors/invoice/versions/1/try", data={"text": self.TEXT}, headers=htmx
        )
        assert response.status_code == 200
        page = response.text
        assert "would route here" in page
        assert "120.00" in page
        assert 'title="worked out by the transform' in page
        assert "not stored" in page

    def test_a_draft_can_be_tried_before_anything_routes_to_it(
        self, signed_in, htmx, fake_model
    ):
        url = _draft(signed_in, htmx)
        response = signed_in.post(url.replace("/edit", "/try"), data={"text": self.TEXT},
                                  headers=htmx)
        assert "would route here" in response.text

    def test_an_uploaded_sample_is_read_like_a_document(self, signed_in, htmx, fake_model):
        from api.routes import processors as api_processors
        from ocr.base import OcrOutput

        class _Engine:
            name = "fake"

            def read(self, data, mime_type):
                return OcrOutput(full_text=TestTrying.TEXT)

        app.dependency_overrides[api_processors.get_ocr_engine] = _Engine
        try:
            from .conftest import TINY_PNG

            response = signed_in.post(
                "/panel/processors/invoice/versions/1/try",
                files={"sample": ("s.png", TINY_PNG, "image/png")},
                headers=htmx,
            )
        finally:
            app.dependency_overrides.pop(api_processors.get_ocr_engine, None)
        assert response.status_code == 200
        assert f"{len(self.TEXT)} characters" in response.text

    def test_nothing_to_try_it_on_is_explained(self, signed_in, htmx, fake_model):
        response = signed_in.post(
            "/panel/processors/invoice/versions/1/try", data={"text": ""}, headers=htmx
        )
        assert response.status_code == 422
        assert "stored document or some text" in response.text


# --- Publishing and retiring (1.5) --------------------------------------------


def _edited_draft(signed_in, htmx, session_factory, **changes) -> int:
    """A draft of invoice from v1 with `changes` applied; returns its version."""
    url = _draft(signed_in, htmx)
    version = int(url.split("/versions/")[1].split("/")[0])
    with session_factory() as s:
        row = authoring.get_version(s, "invoice", version)
        authoring.update_draft(s, "invoice", version, {**row.definition, **changes})
    return version


class TestPublishing:
    def test_the_preview_shows_the_diff_against_the_live_version(
        self, signed_in, htmx, session_factory
    ):
        version = _edited_draft(signed_in, htmx, session_factory, display_name="Edited")
        page = signed_in.get(f"/panel/processors/invoice/versions/{version}/publish").text

        assert "instead of v1" in page
        assert '<span class="add">+display_name: Edited</span>' in page
        assert "Nobody has published since" in page
        assert "passes all 5 of its examples" in page
        assert "changes the transform" not in page  # same code as v1

    def test_new_code_is_called_out_before_confirming(self, signed_in, htmx, session_factory):
        with session_factory() as s:
            transform = dict(authoring.get_version(s, "invoice", 1).definition["transform"])
        transform["code"] = transform["code"] + "\n# reviewed\n"
        version = _edited_draft(signed_in, htmx, session_factory, transform=transform)

        page = signed_in.get(f"/panel/processors/invoice/versions/{version}/publish").text
        assert "new Python will run on the server" in page

    def test_confirming_publishes_and_routes_to_it(self, signed_in, htmx, session_factory):
        version = _edited_draft(signed_in, htmx, session_factory, display_name="Edited")
        response = signed_in.post(
            f"/panel/processors/invoice/versions/{version}/publish", headers=htmx
        )
        assert response.headers["HX-Redirect"] == f"/panel/processors/invoice/versions/{version}"
        with session_factory() as s:
            assert repository.get_active_document_type(s, "invoice").version == version

    def test_failing_examples_hide_the_publish_button(self, signed_in, htmx, session_factory):
        with session_factory() as s:
            transform = dict(authoring.get_version(s, "invoice", 1).definition["transform"])
        transform["code"] = "def transform(data):\n    return data\n"
        version = _edited_draft(signed_in, htmx, session_factory, transform=transform)

        page = signed_in.get(f"/panel/processors/invoice/versions/{version}/publish").text
        assert "fails its examples" in page
        assert "example 1: total should be &#39;120.00&#39;, got null" in page
        assert f'hx-post="/panel/processors/invoice/versions/{version}/publish"' not in page

    def test_a_conflict_is_explained_before_anyone_clicks(self, signed_in, htmx, session_factory):
        mine = _edited_draft(signed_in, htmx, session_factory, display_name="Mine")
        theirs = _edited_draft(signed_in, htmx, session_factory, display_name="Theirs")
        signed_in.post(f"/panel/processors/invoice/versions/{theirs}/publish", headers=htmx)

        page = signed_in.get(f"/panel/processors/invoice/versions/{mine}/publish").text
        assert f"v{theirs} was published after this draft was started" in page
        assert "Publishing would undo it" in page
        assert f'hx-post="/panel/processors/invoice/versions/{mine}/publish"' not in page

    def test_a_conflict_on_confirm_comes_back_as_a_409_beside_the_button(
        self, signed_in, htmx, session_factory
    ):
        """The preview can be minutes old; the confirm checks again."""
        mine = _edited_draft(signed_in, htmx, session_factory, display_name="Mine")
        theirs = _edited_draft(signed_in, htmx, session_factory, display_name="Theirs")
        signed_in.post(f"/panel/processors/invoice/versions/{theirs}/publish", headers=htmx)

        response = signed_in.post(
            f"/panel/processors/invoice/versions/{mine}/publish", headers=htmx
        )
        assert response.status_code == 409
        assert "merge its changes" in response.text

    def test_an_author_cannot_publish(self, _wired, session_factory):
        with session_factory() as s:
            users.create(s, "bo@example.com", PASSWORD, role="author")
            base = authoring.get_version(s, "invoice", 1).definition
            draft = authoring.create_draft(s, base, based_on=1, created_by="bo")
        with TestClient(app, base_url="https://testserver") as bo:
            bo.post("/panel/login", data={"email": "bo@example.com", "password": PASSWORD})
            page = bo.get(f"/panel/processors/invoice/versions/{draft.version}").text
            assert "An admin publishes drafts" in page
            response = bo.get(f"/panel/processors/invoice/versions/{draft.version}/publish")
            assert response.status_code == 403
            assert "needs the admin role" in response.text


class TestRetiring:
    def test_the_confirmation_says_what_stays(self, signed_in):
        page = signed_in.get("/panel/processors/invoice/retire").text
        assert "Nothing is deleted" in page
        assert "start a draft from v1" in page

    def test_retiring_stops_routing(self, signed_in, htmx, session_factory):
        response = signed_in.post("/panel/processors/invoice/retire", headers=htmx)
        assert response.headers["HX-Redirect"] == "/panel/processors/invoice"
        with session_factory() as s:
            assert repository.get_active_document_type(s, "invoice") is None
            assert len(authoring.history(s, "invoice")) == 1

    def test_a_retired_processor_comes_back_by_publishing(self, signed_in, htmx, session_factory):
        signed_in.post("/panel/processors/invoice/retire", headers=htmx)
        version = _edited_draft(signed_in, htmx, session_factory)
        page = signed_in.get(f"/panel/processors/invoice/versions/{version}/publish").text
        assert "publishing brings invoice back" in page
        signed_in.post(f"/panel/processors/invoice/versions/{version}/publish", headers=htmx)
        with session_factory() as s:
            assert repository.get_active_document_type(s, "invoice").version == version

    def test_history_offers_retire_only_to_admins_and_only_when_live(
        self, signed_in, htmx
    ):
        assert "/panel/processors/invoice/retire" in signed_in.get("/panel/processors/invoice").text
        signed_in.post("/panel/processors/invoice/retire", headers=htmx)
        assert "/panel/processors/invoice/retire" not in signed_in.get(
            "/panel/processors/invoice"
        ).text


# --- Draft with AI (1.6) ------------------------------------------------------


class TestDraftWithAi:
    @pytest.fixture
    def drafter(self):
        from api.routes import processors as api_processors

        from .test_drafting import GOOD, FakeDrafter

        fake = FakeDrafter(GOOD, GOOD)
        app.dependency_overrides[api_processors.get_drafter] = lambda: fake
        yield fake
        app.dependency_overrides.pop(api_processors.get_drafter, None)

    def test_the_new_page_offers_it_first(self, signed_in):
        page = signed_in.get("/panel/new").text
        assert page.index("Draft it with AI") < page.index("Or write it yourself")

    def test_a_drafted_processor_links_to_its_review(self, signed_in, htmx, drafter):
        response = signed_in.post(
            "/panel/new/ai", data={"description": "Purchase orders."}, headers=htmx
        )
        assert response.status_code == 200
        assert "Drafted <strong>purchase_order</strong> v1" in response.text
        assert "/panel/processors/purchase_order/versions/1/edit" in response.text
        assert "Check the PO label" in response.text

    def test_a_failed_draft_hands_over_the_yaml_to_finish(self, signed_in, htmx):
        import copy

        from api.routes import processors as api_processors

        from .test_drafting import GOOD, FakeDrafter

        broken = copy.deepcopy(GOOD)
        broken["patterns"] = ["(unclosed"]
        app.dependency_overrides[api_processors.get_drafter] = lambda: FakeDrafter(broken, broken)
        try:
            response = signed_in.post(
                "/panel/new/ai", data={"description": "Purchase orders."}, headers=htmx
            )
        finally:
            app.dependency_overrides.pop(api_processors.get_drafter, None)
        assert "nothing was saved" in response.text
        assert 'hx-post="/panel/new"' in response.text
        assert "name: purchase_order" in response.text

    def test_the_sample_text_reaches_the_model(self, signed_in, htmx, drafter):
        signed_in.post(
            "/panel/new/ai",
            data={"description": "Purchase orders.", "sample_text": "PURCHASE ORDER PO-77"},
            headers=htmx,
        )
        assert "PURCHASE ORDER PO-77" in drafter.calls[0]["user"]
