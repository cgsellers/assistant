"""
The demo client (demo/seed.py): synthetic documents through the real
pipeline, for nothing.
"""

from __future__ import annotations

from collections import Counter
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from api.main import app
from core import spend, tenancy
from core.models import Document, Extraction, User
from demo import seed as demo
from doctypes import registry, seed

pytestmark = pytest.mark.usefixtures("fast_hashing")


@pytest.fixture
def seeded(_wired, session, settings):
    registry.clear_cache()
    seed.sync(session, seed.load_seeds())
    organisation = demo.seed(session)
    yield organisation
    registry.clear_cache()


def statuses(session, organisation) -> Counter:
    return Counter(
        d.status for d in session.query(Document).filter_by(organisation_id=organisation.id)
    )


def test_every_document_ends_where_its_story_says(session, seeded):
    assert statuses(session, seeded) == {"committed": 23, "needs_review": 4, "unsupported": 1}


def test_nothing_was_spent(session, seeded):
    assert {e.model_used for e in session.query(Extraction)} <= {"demo-known-answers", "correction"}
    assert spend.spent_today(session) == Decimal("0")


def test_its_people_sign_in_with_their_roles(seeded):
    for email, role in demo.PEOPLE:
        browser = TestClient(app, base_url="https://testserver")
        me = browser.post("/auth/login", json={"email": email, "password": demo.DEFAULT_PASSWORD})
        assert me.status_code == 200, email
        assert me.json()["role"] == role
        assert me.json()["organisation_name"] == "Acme Demo Ltd"


def test_a_member_corrected_one_and_the_admin_approved_one(client, session, seeded):
    by = Counter(
        entry["by"]
        for document in session.query(Document).filter_by(organisation_id=seeded.id)
        for entry in _history(document)
        if entry["by"]
    )
    assert by == {"member@acme.example": 1, "admin@acme.example": 1}


def test_values_point_at_where_they_were_printed(session, seeded):
    from doctypes.fields import document_fields

    document = (
        session.query(Document)
        .filter(
            Document.organisation_id == seeded.id, Document.original_filename.like("northwind-%")
        )
        .first()
    )
    fields = {f["name"]: f for f in document_fields(session, document)["fields"]}
    assert fields["invoice_number"]["location"]["page"] == 1
    assert fields["total"]["location"] is not None


def test_replacing_starts_again_and_refusing_otherwise(session, seeded, settings):
    with pytest.raises(SystemExit, match="already there"):
        demo.seed(session)
    first = {d.id for d in session.query(Document).filter_by(organisation_id=seeded.id)}

    again = demo.seed(session, replace=True)
    assert again.id == seeded.id
    assert statuses(session, again) == {"committed": 23, "needs_review": 4, "unsupported": 1}
    assert first.isdisjoint(
        d.id for d in session.query(Document).filter_by(organisation_id=again.id)
    )
    assert session.query(User).filter_by(organisation_id=again.id).count() == 3
    # One folder, the demo client's own (core/storage.py).
    assert [p.name for p in (settings.storage_dir / "raw").iterdir()] == [again.id]


def test_another_organisation_is_left_alone(session, seeded):
    other = tenancy.create(session, "Globex", "globex")
    from core import repository

    kept, _ = repository.ingest(
        session, organisation_id=other.id, data=b"%PDF-1.4 kept", mime_type="application/pdf",
        original_filename="kept.pdf",
    )  # fmt: skip
    demo.seed(session, replace=True)
    assert session.get(Document, kept.id) is not None


def test_a_running_worker_never_gets_a_demo_document_to_read(_wired, session, settings):
    """Its jobs are never queued: a worker polling throughout finds nothing of the demo's."""
    from core.models import Job
    from worker import run as worker

    registry.clear_cache()
    seed.sync(session, seed.load_seeds())
    taken = []
    original = demo.repository.ingest

    def ingest_then_poll(*args, **kwargs):
        made = original(*args, **kwargs)
        # A worker polling straight after each upload, as one running would.
        while (job := worker.claim(session, me="real-worker")) is not None:
            taken.append(job)
        return made

    demo.repository.ingest = ingest_then_poll
    try:
        organisation = demo.seed(session)
    finally:
        demo.repository.ingest = original
    assert taken == []
    assert statuses(session, organisation) == {"committed": 23, "needs_review": 4, "unsupported": 1}
    assert session.query(Job).filter_by(status="queued").count() == 0


def _history(document):
    from doctypes.fields import document_fields

    return document_fields(_history.session, document)["history"]


@pytest.fixture(autouse=True)
def _history_session(session):
    _history.session = session
