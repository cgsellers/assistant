"""
A client's settings: its API keys and its people, invitations, and what each
role may do (DESIGN §9.4; api/routes/settings.py, core/people.py).

Through signed-in browsers, as the portal uses them. That other clients'
keys and people stay out of reach is crossed in test_every_route.py.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from api.main import app
from core import tenancy, users
from core.models import Invitation, utcnow

from .conftest import TINY_PNG

pytestmark = pytest.mark.usefixtures("fast_hashing")

PASSWORD = "correct horse battery"


@pytest.fixture
def acme(_wired, session):
    """A client with an admin, a member and a viewer."""
    org = tenancy.create(session, "Acme Ltd", "acme")
    for email, role in (
        ("bo@acme.test", "admin"),
        ("ana@acme.test", "member"),
        ("audit@acme.test", "viewer"),
    ):
        users.create(session, email, PASSWORD, organisation_id=org.id, is_staff=False, role=role)
    return org


class Browser:
    """A signed-in browser that sends its CSRF token, as the portal does."""

    def __init__(self, email: str, password: str = PASSWORD):
        self.client = TestClient(app, base_url="https://testserver")
        me = self.client.post("/auth/login", json={"email": email, "password": password})
        assert me.status_code == 200, me.text
        self.csrf = {"X-CSRF-Token": me.json()["csrf_token"]}

    def get(self, path):
        return self.client.get(path)

    def send(self, method, path, body=None):
        return self.client.request(method, path, json=body, headers=self.csrf)


def invite(admin: Browser, email: str, role: str = "member") -> str:
    response = admin.send("POST", "/settings/members", {"email": email, "role": role})
    assert response.status_code == 201, response.text
    return response.json()["invite_url"].split("token=")[1]


def member(admin: Browser, email: str) -> dict:
    return next(m for m in admin.get("/settings/members").json() if m["email"] == email)


class TestApiKeys:
    def test_a_new_key_is_shown_once_and_works_at_once(self, acme):
        admin = Browser("bo@acme.test")
        response = admin.send("POST", "/settings/keys", {"name": "  NetSuite   production "})
        assert response.status_code == 201
        body = response.json()
        secret = body["secret"]
        assert secret.startswith("lk_") and len(secret) == 46
        assert body["key"]["name"] == "NetSuite production"
        assert body["key"]["prefix"] == secret[:11]
        assert body["key"]["created_by"] == "bo@acme.test"
        assert [k["prefix"] for k in admin.get("/settings/keys").json()] == [secret[:11]]
        assert secret not in admin.get("/settings/keys").text

        machine = TestClient(app, headers={"Authorization": f"Bearer {secret}"})
        upload = machine.post("/documents", files={"file": ("r.png", TINY_PNG, "image/png")})
        assert upload.status_code == 200

    def test_a_revoked_key_stops_working_at_once(self, acme):
        admin = Browser("bo@acme.test")
        made = admin.send("POST", "/settings/keys", {"name": "Old script"}).json()
        assert admin.send("DELETE", f"/settings/keys/{made['key']['id']}").status_code == 204
        assert admin.get("/settings/keys").json() == []
        machine = TestClient(app, headers={"Authorization": f"Bearer {made['secret']}"})
        assert machine.get("/documents").status_code == 401

    def test_only_an_admin_makes_or_revokes_keys(self, acme):
        made = Browser("bo@acme.test").send("POST", "/settings/keys", {"name": "x"}).json()
        staff = Browser("ana@acme.test")
        assert staff.get("/settings/keys").status_code == 200
        refused = staff.send("POST", "/settings/keys", {"name": "mine"})
        assert refused.status_code == 403
        assert refused.json()["detail"] == "Only an admin can change this."
        assert staff.send("DELETE", f"/settings/keys/{made['key']['id']}").status_code == 403

    def test_a_key_needs_a_name(self, acme):
        response = Browser("bo@acme.test").send("POST", "/settings/keys", {"name": "   "})
        assert response.status_code == 422


class TestInvitations:
    def test_an_invitation_becomes_a_signed_in_person_with_the_role_given(self, acme, settings):
        admin = Browser("bo@acme.test")
        response = admin.send(
            "POST", "/settings/members", {"email": "Lee@Acme.test", "role": "viewer"}
        )
        body = response.json()
        assert body["invite_url"].startswith(f"{settings.portal_url}/accept-invite?token=")
        assert body["member"] == {
            "id": body["member"]["id"], "email": "lee@acme.test", "role": "viewer",
            "status": "invited", "last_seen_at": None, "you": False,
        }  # fmt: skip
        token = body["invite_url"].split("token=")[1]

        guest = TestClient(app, base_url="https://testserver")
        offer = guest.post("/auth/invitation", json={"token": token}).json()
        assert (offer["email"], offer["organisation_name"], offer["role"]) == (
            "lee@acme.test", "Acme Ltd", "viewer"
        )  # fmt: skip

        joined = guest.post(
            "/auth/invitation/accept", json={"token": token, "password": "a long new passphrase"}
        )
        assert joined.status_code == 200
        assert joined.json()["role"] == "viewer" and joined.json()["csrf_token"]
        assert guest.get("/auth/me").json()["name"] == "lee@acme.test"
        assert member(admin, "lee@acme.test")["status"] == "active"
        # Spent: a second use is refused.
        assert guest.post("/auth/invitation", json={"token": token}).status_code == 404

    def test_inviting_again_replaces_the_earlier_link(self, acme):
        admin = Browser("bo@acme.test")
        first = invite(admin, "lee@acme.test")
        second = invite(admin, "lee@acme.test", "viewer")
        guest = TestClient(app)
        assert guest.post("/auth/invitation", json={"token": first}).status_code == 404
        assert guest.post("/auth/invitation", json={"token": second}).json()["role"] == "viewer"
        assert [m["email"] for m in admin.get("/settings/members").json()].count(
            "lee@acme.test"
        ) == 1

    def test_a_withdrawn_or_expired_link_does_not_work(self, acme, session):
        admin = Browser("bo@acme.test")
        withdrawn = invite(admin, "lee@acme.test")
        admin.send("DELETE", f"/settings/members/{member(admin, 'lee@acme.test')['id']}")
        expired = invite(admin, "kim@acme.test")
        session.query(Invitation).filter_by(email="kim@acme.test").update(
            {"expires_at": utcnow() - timedelta(minutes=1)}
        )
        session.commit()
        guest = TestClient(app)
        for token in (withdrawn, expired):
            response = guest.post("/auth/invitation", json={"token": token})
            assert response.status_code == 404
            assert (
                response.json()["detail"]
                == "This invitation link doesn't work any more. Ask for a new one."
            )

    @pytest.mark.parametrize(
        ("email", "role", "status", "says"),
        [
            ("ana@acme.test", "member", 409, "ana@acme.test is already in the team."),
            ("not an email", "member", 422, "That doesn't look like an email address."),
            ("new@acme.test", "author", 422, "role must be one of admin, member, viewer"),
        ],
    )
    def test_what_cannot_be_invited_says_why(self, acme, email, role, status, says):
        response = Browser("bo@acme.test").send(
            "POST", "/settings/members", {"email": email, "role": role}
        )
        assert (response.status_code, response.json()["detail"]) == (status, says)

    def test_someone_with_an_account_elsewhere_cannot_be_invited(self, acme, session):
        other = tenancy.create(session, "Globex", "globex")
        users.create(session, "kim@globex.test", PASSWORD, organisation_id=other.id, is_staff=False)
        response = Browser("bo@acme.test").send(
            "POST", "/settings/members", {"email": "kim@globex.test", "role": "member"}
        )
        assert response.status_code == 409
        assert "already has a Ledgerline account elsewhere" in response.json()["detail"]

    def test_a_short_password_is_refused_and_the_link_still_works(self, acme):
        token = invite(Browser("bo@acme.test"), "lee@acme.test")
        guest = TestClient(app)
        response = guest.post("/auth/invitation/accept", json={"token": token, "password": "short"})
        assert response.status_code == 422
        assert guest.post("/auth/invitation", json={"token": token}).status_code == 200


class TestTheTeam:
    def test_everyone_with_their_role_and_who_is_asking(self, acme):
        listed = Browser("ana@acme.test").get("/settings/members").json()
        assert [(m["email"], m["role"], m["status"], m["you"]) for m in listed] == [
            ("bo@acme.test", "admin", "active", False),
            ("ana@acme.test", "member", "active", True),
            ("audit@acme.test", "viewer", "active", False),
        ]

    def test_roles_change_but_never_to_no_admin(self, acme):
        admin = Browser("bo@acme.test")
        bo = member(admin, "bo@acme.test")
        refused = admin.send("PATCH", f"/settings/members/{bo['id']}", {"role": "member"})
        assert refused.status_code == 409
        assert refused.json()["detail"] == (
            "Acme Ltd needs at least one admin. Make someone else an admin first."
        )
        ana = member(admin, "ana@acme.test")
        assert (
            admin.send("PATCH", f"/settings/members/{ana['id']}", {"role": "admin"}).json()["role"]
            == "admin"
        )
        # With two admins, either may step down.
        assert (
            admin.send("PATCH", f"/settings/members/{bo['id']}", {"role": "member"}).status_code
            == 200
        )

    def test_removing_someone_ends_their_sessions_now(self, acme):
        admin = Browser("bo@acme.test")
        ana = Browser("ana@acme.test")
        assert (
            admin.send(
                "DELETE", f"/settings/members/{member(admin, 'ana@acme.test')['id']}"
            ).status_code
            == 204
        )
        assert ana.get("/auth/me").status_code == 401
        signing_in = TestClient(app, base_url="https://testserver").post(
            "/auth/login", json={"email": "ana@acme.test", "password": PASSWORD}
        )
        assert signing_in.status_code == 401
        assert "ana@acme.test" not in [m["email"] for m in admin.get("/settings/members").json()]

    def test_nobody_removes_themselves(self, acme):
        admin = Browser("bo@acme.test")
        response = admin.send("DELETE", f"/settings/members/{member(admin, 'bo@acme.test')['id']}")
        assert (response.status_code, response.json()["detail"]) == (
            409, "You can't remove yourself. Ask another admin."
        )  # fmt: skip

    def test_someone_removed_can_be_invited_back(self, acme):
        admin = Browser("bo@acme.test")
        admin.send("DELETE", f"/settings/members/{member(admin, 'ana@acme.test')['id']}")
        token = invite(admin, "ana@acme.test", "viewer")
        back = TestClient(app, base_url="https://testserver").post(
            "/auth/invitation/accept", json={"token": token, "password": "another long passphrase"}
        )
        assert back.status_code == 200 and back.json()["role"] == "viewer"
        assert member(admin, "ana@acme.test")["status"] == "active"


class TestWhatAViewerMayDo:
    def test_a_viewer_reads_and_changes_nothing(self, acme):
        viewer = Browser("audit@acme.test")
        assert viewer.get("/documents").status_code == 200
        assert viewer.get("/settings/members").status_code == 200
        upload = viewer.client.post(
            "/documents", files={"file": ("r.png", TINY_PNG, "image/png")}, headers=viewer.csrf
        )
        assert upload.status_code == 403
        assert upload.json()["detail"] == "Your role can see documents but not change them."
        assert viewer.send("POST", "/settings/keys", {"name": "x"}).status_code == 403

    def test_a_viewer_cannot_correct_a_document(self, acme, session):
        from core import repository

        document, _ = repository.ingest(
            session, organisation_id=acme.id, data=TINY_PNG, mime_type="image/png",
            original_filename=None,
        )  # fmt: skip
        viewer = Browser("audit@acme.test")
        response = viewer.send(
            "PATCH", f"/documents/{document.id}/fields", {"revision": 1, "values": {}}
        )
        assert response.status_code == 403
