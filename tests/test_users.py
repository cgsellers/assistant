"""
People: passwords, sessions, CSRF and roles.

The things worth pinning are the ones an attacker would probe: that a stolen
table does not hand over passwords, that sign-in failures all look alike,
that guessing is cut off, that a cookie alone cannot change anything, and
that an author cannot put code live.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from api.main import app
from core import auth, users
from core.models import User, UserSession, utcnow

from .conftest import demo_org_id

PASSWORD = "correct horse battery"


pytestmark = pytest.mark.usefixtures("fast_hashing")


@pytest.fixture
def make_user(session_factory):
    def _make(email="ana@example.com", role="author", password=PASSWORD):
        with session_factory() as s:
            return users.create(
                s, email, password, role=role, organisation_id=demo_org_id(s), is_staff=True,
            )

    return _make


@pytest.fixture
def browser(_wired):
    """
    A client with no key: it authenticates the way a person's browser does.
    Over https, because the cookie is Secure and a correct client will not
    send it over plain http.
    """
    with TestClient(app, base_url="https://testserver") as c:
        yield c


def _sign_in(client, email="ana@example.com", password=PASSWORD):
    response = client.post("/auth/login", json={"email": email, "password": password})
    return response, response.json().get("csrf_token")


SPEC = {
    "name": "till",
    "matches": {"keywords": ["total"]},
    "fields": {"total": {"type": "money", "description": "The total."}},
}


class TestPasswords:
    def test_the_password_is_never_stored(self, session_factory, make_user):
        make_user()
        with session_factory() as s:
            stored = s.query(User).one().password_hash
        assert PASSWORD not in stored
        assert stored.startswith("$argon2id$")

    def test_a_short_password_is_refused(self, session):
        with pytest.raises(ValueError, match="at least 12"):
            users.create(
                session, "a@x.com", "short", organisation_id=demo_org_id(session), is_staff=True,
            )

    def test_emails_are_one_person_whatever_their_case(self, session, make_user):
        make_user("Ana@Example.com")
        with pytest.raises(ValueError, match="already has an account"):
            users.create(
                session, "ana@example.com", PASSWORD, organisation_id=demo_org_id(session),
                is_staff=True,
            )

    def test_an_unknown_role_is_refused(self, session):
        with pytest.raises(ValueError, match="role"):
            users.create(
                session, "a@x.com", PASSWORD, role="owner", organisation_id=demo_org_id(session),
                is_staff=True,
            )


class TestSigningIn:
    def test_a_right_password_sets_an_httponly_cookie(self, browser, make_user):
        make_user()
        response, csrf = _sign_in(browser)

        assert response.status_code == 200
        cookie = response.headers["set-cookie"]
        assert auth.SESSION_COOKIE in cookie
        assert "HttpOnly" in cookie
        assert "SameSite=lax" in cookie
        assert csrf

    def test_every_failure_looks_the_same(self, browser, make_user, session_factory):
        """Wrong password, unknown email, disabled: one answer, so no enumeration."""
        make_user()
        make_user("off@example.com")
        with session_factory() as s:
            users.disable(s, "off@example.com")

        answers = {
            _sign_in(browser, password="wrong password!!")[0].json()["detail"],
            _sign_in(browser, email="nobody@example.com")[0].json()["detail"],
            _sign_in(browser, email="off@example.com")[0].json()["detail"],
        }
        assert answers == {"wrong email or password"}

    def test_ten_failures_lock_the_account_even_against_the_right_password(
        self, browser, make_user
    ):
        make_user()
        for _ in range(users.MAX_FAILURES):
            _sign_in(browser, password="wrong password!!")

        assert _sign_in(browser)[0].status_code == 401

    def test_the_lock_ends(self, browser, make_user, session_factory):
        make_user()
        for _ in range(users.MAX_FAILURES):
            _sign_in(browser, password="wrong password!!")
        with session_factory() as s:
            s.query(User).one().locked_until = utcnow() - timedelta(seconds=1)
            s.commit()

        assert _sign_in(browser)[0].status_code == 200

    def test_a_success_resets_the_count(self, browser, make_user, session_factory):
        make_user()
        _sign_in(browser, password="wrong password!!")
        _sign_in(browser)
        with session_factory() as s:
            assert s.query(User).one().failed_logins == 0


class TestSessions:
    def test_a_session_reads_without_a_csrf_token(self, browser, make_user):
        make_user()
        _sign_in(browser)
        me = browser.get("/auth/me").json()
        assert (me["kind"], me["name"], me["role"]) == ("user", "ana@example.com", "author")

    def test_a_secure_cookie_is_not_sent_over_plain_http(self, _wired, make_user):
        make_user()
        with TestClient(app) as plain:
            _sign_in(plain)
            assert plain.get("/auth/me").status_code == 401

    def test_only_the_tokens_hash_is_stored(self, browser, make_user, session_factory):
        make_user()
        response, _ = _sign_in(browser)
        token = response.cookies[auth.SESSION_COOKIE]
        with session_factory() as s:
            assert s.query(UserSession).one().token_hash != token

    def test_signing_out_ends_it_on_the_server(self, browser, make_user):
        make_user()
        response, csrf = _sign_in(browser)
        token = response.cookies[auth.SESSION_COOKIE]

        assert browser.post("/auth/logout", headers={auth.CSRF_HEADER: csrf}).status_code == 204
        # The old cookie, replayed, is dead -- not merely deleted from one browser.
        browser.cookies.set(auth.SESSION_COOKIE, token)
        assert browser.get("/auth/me").status_code == 401

    def test_an_expired_session_is_refused(self, browser, make_user, session_factory):
        make_user()
        _sign_in(browser)
        with session_factory() as s:
            s.query(UserSession).one().expires_at = utcnow() - timedelta(seconds=1)
            s.commit()
        assert browser.get("/auth/me").status_code == 401

    def test_disabling_a_user_ends_their_sessions_now(self, browser, make_user, session_factory):
        make_user()
        _sign_in(browser)
        with session_factory() as s:
            users.disable(s, "ana@example.com")
        assert browser.get("/auth/me").status_code == 401

    def test_a_new_password_ends_every_session(self, browser, make_user, session_factory):
        make_user()
        _sign_in(browser)
        with session_factory() as s:
            users.set_password(s, "ana@example.com", "a different passphrase")
        assert browser.get("/auth/me").status_code == 401


class TestCsrf:
    """The cookie is sent by the browser automatically; the token is not."""

    def test_a_cookie_alone_cannot_change_anything(self, browser, make_user):
        make_user()
        _sign_in(browser)
        response = browser.post("/processors", json={"definition": SPEC})
        assert response.status_code == 403
        assert "CSRF" in response.json()["detail"]

    def test_a_wrong_token_is_refused(self, browser, make_user):
        make_user()
        _sign_in(browser)
        response = browser.post(
            "/processors", json={"definition": SPEC}, headers={auth.CSRF_HEADER: "guess"}
        )
        assert response.status_code == 403

    def test_the_right_token_gets_through(self, browser, make_user):
        make_user()
        _, csrf = _sign_in(browser)
        response = browser.post(
            "/processors", json={"definition": SPEC}, headers={auth.CSRF_HEADER: csrf}
        )
        assert response.status_code == 201
        assert response.json()["created_by"] == "user:ana@example.com"

    def test_a_key_needs_no_token(self, client):
        """A browser never attaches a key on its own, so a keyed request is not forgeable."""
        assert client.post("/processors", json={"definition": SPEC}).status_code == 201


class TestRoles:
    def test_an_author_can_draft_but_not_publish(self, browser, make_user):
        make_user(role="author")
        _, csrf = _sign_in(browser)
        headers = {auth.CSRF_HEADER: csrf}
        browser.post("/processors", json={"definition": SPEC}, headers=headers)

        response = browser.post("/processors/till/versions/1/publish", headers=headers)
        assert response.status_code == 403
        assert "admin" in response.json()["detail"]

    def test_an_admin_can_publish_and_retire(self, browser, make_user):
        make_user(role="admin")
        _, csrf = _sign_in(browser)
        headers = {auth.CSRF_HEADER: csrf}
        browser.post("/processors", json={"definition": SPEC}, headers=headers)

        published = browser.post("/processors/till/versions/1/publish", headers=headers)
        assert published.status_code == 200
        assert browser.post("/processors/till/retire", headers=headers).status_code == 200

    def test_keys_have_roles_too(self, _wired, session_factory):
        with session_factory() as s:
            _, secret = auth.create(  # author by default
                s, "drafting agent", organisation_id=demo_org_id(s),
            )
        with TestClient(app, headers={"Authorization": f"Bearer {secret}"}) as c:
            c.post("/processors", json={"definition": SPEC})
            assert c.post("/processors/till/versions/1/publish").status_code == 403

    def test_reads_are_open_to_any_role(self, browser, make_user):
        make_user(role="author")
        _sign_in(browser)
        assert browser.get("/processors").status_code == 200
