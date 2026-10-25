"""
Tenant isolation through the real API (DESIGN §9.3, step 4.2).

Two client organisations, each with a key, a user and a processed document,
plus the team. Every way of reaching another organisation's document must
answer exactly as if it did not exist; the processor API and the admin panel
must be the team's alone. Step 4.5 widens this to every route automatically.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from api.main import app
from core import auth, repository, tenancy, users
from core.models import OcrResult, User, UserSession

from .conftest import demo_org_id

pytestmark = pytest.mark.usefixtures("fast_hashing")

PASSWORD = "correct horse battery"


@pytest.fixture
def world(_wired, session):
    """Acme and Globex: a key, a user and a document each. The team: staff and a key."""
    out = {}
    for slug in ("acme", "globex"):
        org = tenancy.create(session, slug.title(), slug)
        document, _ = repository.ingest(
            session, organisation_id=org.id, data=slug.encode() * 9,
            mime_type="image/png", original_filename=f"{slug}.png",
        )
        session.add(OcrResult(
            document_id=document.id, engine="fake", engine_version="1", params={},
            full_text=f"{slug} receipt", layout_blocks=[], mean_confidence=None,
        ))
        repository.record_extraction(
            session, document_id=document.id, doc_type="receipt", schema_version=1,
            model_used="m", raw_json={"vendor": slug}, cost_usd=Decimal("0.0100"),
        )
        _, key = auth.create(session, f"{slug} erp", organisation_id=org.id)
        users.create(
            session, f"bo@{slug}.test", PASSWORD, organisation_id=org.id, is_staff=False
        )
        out[slug] = {"org": org.id, "document": document.id, "key": key}
    users.create(
        session, "ana@ledgerline.test", PASSWORD,
        organisation_id=demo_org_id(session), is_staff=True, role="admin",
    )
    _, team_key = auth.create(
        session, "team script", organisation_id=demo_org_id(session), role="admin"
    )
    out["team_key"] = team_key
    return out


def _with_key(key: str) -> TestClient:
    return TestClient(app, headers={"Authorization": f"Bearer {key}"})


DOCUMENT_ROUTES = ["", "/ocr", "/result", "/file"]


class TestAnotherOrganisationsDocument:
    @pytest.mark.parametrize("suffix", DOCUMENT_ROUTES)
    def test_is_answered_exactly_as_a_missing_one(self, world, suffix):
        with _with_key(world["acme"]["key"]) as acme:
            theirs = acme.get(f"/documents/{world['globex']['document']}{suffix}")
            missing = acme.get(f"/documents/no-such-document{suffix}")
        assert theirs.status_code == 404
        assert theirs.json() == missing.json()  # nothing says it exists

    @pytest.mark.parametrize("suffix", DOCUMENT_ROUTES)
    def test_while_its_own_is_there(self, world, suffix):
        with _with_key(world["acme"]["key"]) as acme:
            assert acme.get(f"/documents/{world['acme']['document']}{suffix}").status_code == 200

    def test_the_cost_report_is_only_its_own(self, world):
        with _with_key(world["acme"]["key"]) as acme:
            report = acme.get("/costs").json()
        assert report["documents"] == 1
        assert report["total_usd"] == "0.010000"

    def test_a_trial_cannot_reach_it_either(self, world, session):
        """A team key trial-running on a client's document id: not the team's to see."""
        from doctypes import seed

        seed.sync(session, seed.load_seeds())
        with _with_key(world["team_key"]) as team:
            response = team.post(
                "/processors/receipt/versions/1/trial",
                json={"document_id": world["acme"]["document"]},
            )
        assert response.status_code == 404

    def test_a_key_is_never_staff_even_the_teams(self, world):
        with _with_key(world["team_key"]) as team:
            assert team.get(f"/documents/{world['acme']['document']}").status_code == 404


class TestTheTeamsTools:
    @pytest.mark.parametrize("method,path,body", [
        ("get", "/processors", None),
        ("get", "/processors/receipt", None),
        ("post", "/processors", {"yaml": "name: x"}),
        ("post", "/processors/draft-with-ai", {"description": "x"}),
        ("post", "/processors/receipt/retire", None),
    ])
    def test_the_processor_api_refuses_a_clients_key(self, world, method, path, body):
        with _with_key(world["acme"]["key"]) as acme:
            response = getattr(acme, method)(path, **({"json": body} if body else {}))
        assert response.status_code == 403
        assert "Ledgerline team" in response.json()["detail"]

    def test_the_teams_key_may_use_it(self, world):
        with _with_key(world["team_key"]) as team:
            assert team.get("/processors").status_code == 200

    def test_a_client_user_cannot_sign_in_to_the_panel(self, world, session):
        with TestClient(app, base_url="https://testserver") as browser:
            response = browser.post(
                "/panel/login", data={"email": "bo@acme.test", "password": PASSWORD}
            )
            assert response.status_code == 403
            assert "for the Ledgerline team" in response.text
            # And no session was left behind to use elsewhere.
            assert browser.get("/auth/me").status_code == 401
        bo = session.scalar(select(User).where(User.email == "bo@acme.test"))
        live = session.scalars(
            select(UserSession).where(
                UserSession.user_id == bo.id, UserSession.revoked_at.is_(None)
            )
        ).all()
        assert live == []

    def test_a_client_user_is_refused_by_panel_pages_however_they_got_in(self, world):
        """Signed in through the API -- the future portal's door -- not the panel's."""
        with TestClient(app, base_url="https://testserver") as browser:
            browser.post("/auth/login", json={"email": "bo@acme.test", "password": PASSWORD})
            assert browser.get("/panel/documents").status_code == 403

    def test_team_staff_see_every_organisation(self, world):
        with TestClient(app, base_url="https://testserver") as browser:
            browser.post(
                "/panel/login", data={"email": "ana@ledgerline.test", "password": PASSWORD}
            )
            page = browser.get("/panel/documents").text
        assert "acme.png" in page and "globex.png" in page


class TestStaffIsTheTeamsAlone:
    def test_a_client_user_cannot_be_made_staff(self, world, session):
        with pytest.raises(ValueError, match="Ledgerline team"):
            users.create(
                session, "eve@acme.test", PASSWORD,
                organisation_id=world["acme"]["org"], is_staff=True,
            )

    def test_a_staff_flag_set_some_other_way_grants_nothing(self, world, session):
        """Checked on every request, not only when the account was made."""
        bo = session.scalar(select(User).where(User.email == "bo@acme.test"))
        bo.is_staff = True
        session.commit()
        with TestClient(app, base_url="https://testserver") as browser:
            browser.post("/auth/login", json={"email": "bo@acme.test", "password": PASSWORD})
            me = browser.get("/auth/me").json()
            other = browser.get(f"/documents/{world['globex']['document']}")
        assert me["is_staff"] is False
        assert other.status_code == 404
