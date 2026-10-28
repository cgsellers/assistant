"""
The daily spending cap (core/spend.py): new uploads refused, queued reading
put off until tomorrow, once today's model spend reaches MAX_DAILY_SPEND_USD.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest

from core import repository, spend, tenancy
from core.models import Extraction, Job, utcnow
from core.status import DocStatus, JobStatus
from worker import run as worker

from .conftest import TINY_PNG, FakeExtractor, demo_org_id, receipt_definition
from .test_extract_job import _shared, claim_extract
from .test_extract_job import ready as _ready

ready = _ready


@pytest.fixture
def capped(settings, monkeypatch):
    """A cap of a dollar a day."""
    monkeypatch.setattr(settings, "max_daily_spend_usd", Decimal("1.00"))
    return settings


def spent(session, organisation_id: str, cost: str, *, days_ago: int = 0) -> None:
    """A billed call, as the cost report would count it."""
    document, _ = repository.ingest(
        session,
        organisation_id=organisation_id,
        data=TINY_PNG + f"{cost}{days_ago}{organisation_id}".encode(),
        mime_type="image/png",
        original_filename=None,
    )
    extraction = repository.record_extraction(
        session,
        document_id=document.id,
        doc_type="receipt",
        schema_version=1,
        model_used="m",
        raw_json={},
        cost_usd=Decimal(cost),
    )
    extraction.created_at = utcnow() - timedelta(days=days_ago)
    session.commit()


def upload(client, data: bytes):
    return client.post("/documents", files={"file": ("r.png", data, "image/png")})


class TestTheCap:
    def test_without_a_cap_nothing_is_ever_refused(self, client, session, settings):
        spent(session, demo_org_id(session), "500.00")
        assert upload(client, TINY_PNG + b"new").status_code == 200

    def test_under_the_cap_uploads_go_through(self, client, session, capped):
        spent(session, demo_org_id(session), "0.40")
        assert upload(client, TINY_PNG + b"new").status_code == 200

    def test_at_the_cap_a_new_upload_is_refused_until_tomorrow(self, client, session, capped):
        spent(session, demo_org_id(session), "1.00")
        response = upload(client, TINY_PNG + b"new")
        assert response.status_code == 429
        assert response.json()["detail"] == (
            "Ledgerline has reached today's limit for reading documents. Try again tomorrow."
        )
        assert 0 < int(response.headers["retry-after"]) <= 86_400

    def test_a_file_already_here_still_answers_since_it_costs_nothing(
        self, client, session, capped
    ):
        first = upload(client, TINY_PNG + b"seen before").json()
        spent(session, demo_org_id(session), "1.00")
        again = upload(client, TINY_PNG + b"seen before")
        assert again.status_code == 200
        assert again.json()["id"] == first["id"] and again.json()["duplicate"] is True

    def test_the_cap_is_on_the_whole_bill_not_one_client(self, client, session, capped):
        other = tenancy.create(session, "Globex", "globex")
        spent(session, other.id, "1.00")
        assert upload(client, TINY_PNG + b"new").status_code == 429

    def test_yesterdays_spend_does_not_count(self, client, session, capped):
        spent(session, demo_org_id(session), "50.00", days_ago=1)
        assert upload(client, TINY_PNG + b"new").status_code == 200

    def test_tomorrow_is_the_next_midnight_utc(self):
        reset = spend.tomorrow()
        assert (reset.hour, reset.minute, reset.second) == (0, 0, 0)
        assert timedelta(0) < reset - utcnow() <= timedelta(days=1)


def test_an_empty_setting_means_no_cap(monkeypatch, settings):
    """As compose passes it when unset: `MAX_DAILY_SPEND_USD: ${MAX_DAILY_SPEND_USD:-}`."""
    from core.config import get_settings

    monkeypatch.setenv("MAX_DAILY_SPEND_USD", "")
    get_settings.cache_clear()
    assert get_settings().max_daily_spend_usd is None
    monkeypatch.setenv("MAX_DAILY_SPEND_USD", "2.50")
    get_settings.cache_clear()
    assert get_settings().max_daily_spend_usd == Decimal("2.50")


class TestTheWorker:
    def test_queued_reading_waits_for_tomorrow_without_calling_the_model(
        self, session, ready, capped
    ):
        _shared(session, receipt_definition())
        document, _ = ready()
        spent(session, demo_org_id(session), "1.00")
        extractor = FakeExtractor({"total": "9,35"})

        job = claim_extract(session)
        attempts_before_claim = job.attempts - 1
        worker.run_extract_job(session, job, extractor)

        session.refresh(document)
        assert extractor.calls == []
        assert job.status == JobStatus.QUEUED
        assert job.run_after == spend.tomorrow()
        assert job.attempts == attempts_before_claim
        assert "spending limit" in job.last_error
        assert document.status == DocStatus.CLASSIFIED
        assert session.query(Extraction).filter_by(document_id=document.id).count() == 0
        assert session.get(Job, job.id).status == JobStatus.QUEUED
