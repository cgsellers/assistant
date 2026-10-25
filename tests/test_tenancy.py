"""
Tenancy, part 1: everything has an owner (DESIGN §9.3).

What is pinned here is ownership itself -- who a document, its derived rows,
a key and a user belong to -- and the duplicate rule, which before tenancy
handed one client another's document. Seeing only your own (4.2), the
database enforcing it (4.3) and routing per client (4.4) come next.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from api.main import app
from core import auth, repository, tenancy, users
from core.models import DocumentEvent, Extraction, Job, OcrResult, Transaction

from .conftest import TINY_PNG, demo_org_id

pytestmark = pytest.mark.usefixtures("fast_hashing")


@pytest.fixture
def acme(session):
    return tenancy.create(session, "Acme Ltd", "acme")


@pytest.fixture
def globex(session):
    return tenancy.create(session, "Globex", "globex")


def _key_client(session, organisation) -> TestClient:
    _, secret = auth.create(session, "erp", organisation_id=organisation.id)
    return TestClient(app, headers={"Authorization": f"Bearer {secret}"})


class TestOwnership:
    def test_an_upload_belongs_to_the_keys_organisation(self, _wired, session, acme):
        with _key_client(session, acme) as erp:
            body = erp.post("/documents", files={"file": ("a.png", TINY_PNG, "image/png")}).json()
        assert repository.get_document(session, body["id"]).organisation_id == acme.id

    def test_with_auth_off_uploads_belong_to_the_team(self, _wired, session, monkeypatch):
        monkeypatch.setattr(
            "core.auth.get_settings", lambda: type("S", (), {"require_auth": False})()
        )
        with TestClient(app) as local:
            body = local.post("/documents", files={"file": ("a.png", TINY_PNG, "image/png")}).json()
        assert repository.get_document(session, body["id"]).organisation_id == demo_org_id(session)

    def test_everything_made_from_a_document_follows_its_owner(self, session, acme):
        document, _ = repository.ingest(
            session, organisation_id=acme.id, data=TINY_PNG, mime_type="image/png",
            original_filename="a.png",
        )
        session.add(OcrResult(
            document_id=document.id, engine="fake", engine_version="1", params={},
            full_text="x", layout_blocks=[], mean_confidence=None,
        ))
        extraction = repository.record_extraction(
            session, document_id=document.id, doc_type="receipt", schema_version=1,
            model_used="fake", raw_json={},
        )
        repository.record_transaction(
            session, extraction=extraction,
            values={"source_type": "receipt", "direction": "out", "amount": Decimal("1")},
        )
        for model in (OcrResult, DocumentEvent, Job, Extraction, Transaction):
            owners = {row.organisation_id for row in session.query(model)}
            assert owners == {acme.id}, model.__name__


class TestDuplicates:
    """The leak this step closes."""

    def test_another_organisations_identical_file_is_not_a_duplicate(self, session, acme, globex):
        first, created_first = repository.ingest(
            session, organisation_id=acme.id, data=TINY_PNG, mime_type="image/png",
            original_filename="acme.png",
        )
        second, created_second = repository.ingest(
            session, organisation_id=globex.id, data=TINY_PNG, mime_type="image/png",
            original_filename="globex.png",
        )
        assert created_first and created_second
        assert first.id != second.id  # Globex never gets Acme's document
        assert second.organisation_id == globex.id
        assert first.storage_key == second.storage_key  # one copy of the bytes on disk

    def test_the_same_organisation_uploading_twice_is_still_a_duplicate(self, session, acme):
        first, _ = repository.ingest(
            session, organisation_id=acme.id, data=TINY_PNG, mime_type="image/png",
            original_filename="a.png",
        )
        again, created = repository.ingest(
            session, organisation_id=acme.id, data=TINY_PNG, mime_type="image/png",
            original_filename="a.png",
        )
        assert (created, again.id) == (False, first.id)

    def test_over_the_api_too(self, _wired, session, acme, globex):
        upload = {"file": ("same.png", TINY_PNG, "image/png")}
        with _key_client(session, acme) as a, _key_client(session, globex) as g:
            first = a.post("/documents", files=upload).json()
            second = g.post("/documents", files=upload).json()
        assert second["id"] != first["id"]
        assert second["duplicate"] is False


class TestWhoIsStaff:
    def test_a_key_carries_its_organisation_and_is_never_staff(self, _wired, session, acme):
        with _key_client(session, acme) as erp:
            me = erp.get("/auth/me").json()
        assert (me["kind"], me["organisation_id"], me["is_staff"]) == ("key", acme.id, False)

    def test_a_signed_in_user_carries_theirs(self, _wired, session, acme):
        users.create(
            session, "ana@ledgerline.test", "correct horse battery",
            organisation_id=demo_org_id(session), is_staff=True,
        )
        users.create(
            session, "bo@acme.test", "correct horse battery",
            organisation_id=acme.id, is_staff=False,
        )
        seen = {}
        for email in ("ana@ledgerline.test", "bo@acme.test"):
            with TestClient(app, base_url="https://testserver") as browser:
                browser.post(
                    "/auth/login", json={"email": email, "password": "correct horse battery"}
                )
                me = browser.get("/auth/me").json()
                seen[email] = (me["organisation_id"], me["is_staff"])
        assert seen == {
            "ana@ledgerline.test": (demo_org_id(session), True),
            "bo@acme.test": (acme.id, False),
        }


class TestOrganisations:
    def test_the_demo_organisation_is_found_not_duplicated(self, session):
        first = tenancy.demo_organisation(session)
        assert tenancy.demo_organisation(session).id == first.id
        assert first.name == "Ledgerline demo"

    def test_a_slug_is_checked(self, session):
        with pytest.raises(ValueError, match="lowercase"):
            tenancy.create(session, "Bad", "Not A Slug")

    def test_two_organisations_cannot_share_a_slug(self, session, acme):
        with pytest.raises(ValueError, match="already exists"):
            tenancy.create(session, "Acme Two", "acme")
