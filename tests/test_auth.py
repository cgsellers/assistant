"""
API keys.

Two things under test: that the door is shut, and that shutting it did not
quietly become optional. The rest of the suite runs authenticated, so these
are the tests that would notice if the dependency stopped being applied.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from api.main import app
from core import auth
from core.config import get_settings
from core.models import ApiKey

from .conftest import TINY_PNG, demo_org_id


def _upload(client: TestClient):
    return client.post("/documents", files={"file": ("a.png", TINY_PNG, "image/png")})


class TestTheDoorIsShut:
    def test_no_credentials_is_refused(self, anon_client):
        assert _upload(anon_client).status_code == 401

    def test_a_wrong_key_is_refused(self, _wired):
        with TestClient(app, headers={"Authorization": "Bearer lk_not-a-real-key"}) as c:
            assert _upload(c).status_code == 401

    def test_a_revoked_key_is_refused(self, _wired, session_factory):
        with session_factory() as s:
            row, secret = auth.create(s, "temporary", organisation_id=demo_org_id(s))
            auth.revoke(s, row.prefix)

        with TestClient(app, headers={"Authorization": f"Bearer {secret}"}) as c:
            assert _upload(c).status_code == 401

    def test_a_valid_key_gets_in(self, client):
        assert _upload(client).status_code == 200

    def test_reads_are_shut_too_not_just_writes(self, anon_client):
        assert anon_client.get("/documents/anything").status_code == 401

    def test_the_refusal_says_how_to_authenticate(self, anon_client):
        response = _upload(anon_client)
        assert response.headers["www-authenticate"] == "Bearer"
        assert "Authorization: Bearer" in response.json()["detail"]

    def test_absent_unknown_and_revoked_are_indistinguishable(
        self, _wired, session_factory, anon_client
    ):
        """
        Telling a caller which it was tells an attacker whether a key exists,
        and tells a legitimate caller nothing "check your key" does not.
        """
        with session_factory() as s:
            row, revoked = auth.create(s, "temporary", organisation_id=demo_org_id(s))
            auth.revoke(s, row.prefix)

        bodies = {_upload(anon_client).json()["detail"]}
        for header in (f"Bearer {revoked}", "Bearer lk_nonsense"):
            with TestClient(app, headers={"Authorization": header}) as c:
                bodies.add(_upload(c).json()["detail"])

        assert len(bodies) == 1


class TestWhereTheKeyMayBe:
    def test_bearer_is_accepted(self, _wired, api_key):
        with TestClient(app, headers={"Authorization": f"Bearer {api_key}"}) as c:
            assert _upload(c).status_code == 200

    def test_x_api_key_is_accepted_too(self, _wired, api_key):
        """Enough clients reach for it that rejecting a correct key is unkind."""
        with TestClient(app, headers={"X-API-Key": api_key}) as c:
            assert _upload(c).status_code == 200

    def test_a_query_parameter_is_not(self, _wired, api_key):
        """Query strings land in access logs and browser history."""
        with TestClient(app) as c:
            response = c.post(
                f"/documents?api_key={api_key}",
                files={"file": ("a.png", TINY_PNG, "image/png")},
            )
        assert response.status_code == 401

    def test_a_malformed_authorization_header_is_refused(self, _wired, api_key):
        with TestClient(app, headers={"Authorization": api_key}) as c:  # no scheme
            assert _upload(c).status_code == 401


class TestPublicPaths:
    def test_health_answers_without_a_key(self, anon_client):
        """
        A load balancer cannot hold a credential. Refusing it would make an
        unauthenticated deploy look healthy and an authenticated one dead.
        """
        assert anon_client.get("/health").status_code == 200

    def test_the_docs_are_open(self, anon_client):
        assert anon_client.get("/openapi.json").status_code == 200

    def test_opening_a_path_takes_an_explicit_edit(self):
        """Not a decorator argument somebody can add without review."""
        assert "/health" in auth.PUBLIC_PATHS
        assert "/documents" not in auth.PUBLIC_PATHS


class TestANewRouteIsProtectedByDefault:
    """
    The dependency is on the app, not the router. A route added later is
    covered because nobody did anything, rather than exposed because somebody
    forgot.
    """

    def test_a_route_added_without_thinking_about_auth_is_still_shut(
        self, anon_client
    ):
        @app.get("/_added_later")
        def _later() -> dict:
            return {"secret": "should not be reachable"}

        try:
            assert anon_client.get("/_added_later").status_code == 401
        finally:
            app.router.routes[:] = [
                r for r in app.router.routes if getattr(r, "path", None) != "/_added_later"
            ]


class TestStorage:
    def test_the_secret_is_never_stored(self, session_factory):
        with session_factory() as s:
            _, secret = auth.create(s, "k", organisation_id=demo_org_id(s))
            rows = s.scalars(select(ApiKey)).all()

        stored = " ".join(f"{r.key_hash} {r.prefix} {r.name}" for r in rows)
        assert secret not in stored

    def test_only_a_prefix_is_recoverable(self, session_factory):
        with session_factory() as s:
            row, secret = auth.create(s, "k", organisation_id=demo_org_id(s))

        assert secret.startswith(row.prefix)
        assert len(row.prefix) < len(secret)

    def test_keys_are_distinct(self):
        assert len({auth.generate() for _ in range(50)}) == 50

    def test_keys_are_recognisable(self):
        assert auth.generate().startswith("lk_")

    def test_the_hash_is_stable(self):
        key = auth.generate()
        assert auth.fingerprint(key) == auth.fingerprint(key)

    def test_two_keys_do_not_collide(self):
        assert auth.fingerprint(auth.generate()) != auth.fingerprint(auth.generate())


class TestLifecycle:
    def test_last_used_is_recorded(self, session_factory, _wired, api_key):
        with TestClient(app, headers={"Authorization": f"Bearer {api_key}"}) as c:
            _upload(c)

        with session_factory() as s:
            assert s.scalars(select(ApiKey)).one().last_used_at is not None

    def test_it_is_not_rewritten_on_every_request(self, session_factory, _wired, api_key):
        """Every GET would otherwise be a write, and SQLite serialises writers."""
        with TestClient(app, headers={"Authorization": f"Bearer {api_key}"}) as c:
            c.get("/documents/nope")
            with session_factory() as s:
                first = s.scalars(select(ApiKey)).one().last_used_at
            c.get("/documents/nope")

        with session_factory() as s:
            assert s.scalars(select(ApiKey)).one().last_used_at == first

    def test_revoking_is_idempotent(self, session_factory):
        with session_factory() as s:
            row, _ = auth.create(s, "k", organisation_id=demo_org_id(s))
            auth.revoke(s, row.prefix)
            first = s.scalars(select(ApiKey)).one().revoked_at
            auth.revoke(s, row.prefix)

            assert s.scalars(select(ApiKey)).one().revoked_at == first

    def test_revoking_keeps_the_row(self, session_factory):
        """So "what was this key doing before we killed it" stays answerable."""
        with session_factory() as s:
            row, _ = auth.create(s, "k", organisation_id=demo_org_id(s))
            auth.revoke(s, row.prefix)

            assert s.scalars(select(ApiKey)).one().id == row.id

    def test_revoking_an_unknown_key_says_so(self, session_factory):
        with session_factory() as s:
            assert auth.revoke(s, "lk_nothing") is None


class TestTheEscapeHatch:
    def test_auth_can_be_turned_off_for_local_work(
        self, session_factory, _wired, monkeypatch
    ):
        monkeypatch.setattr(
            "core.auth.get_settings",
            lambda: type("S", (), {"require_auth": False})(),
        )
        with TestClient(app) as c:
            assert _upload(c).status_code == 200

    def test_it_is_on_by_default(self):
        get_settings.cache_clear()
        assert get_settings().require_auth is True
        get_settings.cache_clear()


class TestAuthenticateDirectly:
    def test_an_empty_key_is_rejected_without_a_query(self, session):
        assert auth.authenticate(session, "") is None
        assert auth.authenticate(session, None) is None

    def test_a_good_key_returns_its_row(self, session):
        row, secret = auth.create(session, "k", organisation_id=demo_org_id(session))
        assert auth.authenticate(session, secret).id == row.id


@pytest.mark.parametrize("path", ["/health", "/openapi.json"])
def test_public_paths_stay_public(anon_client, path):
    assert anon_client.get(path).status_code == 200
