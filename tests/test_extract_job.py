"""
The extract stage in isolation.

test_pipeline covers the happy path end to end; this covers what the stage
does when things go wrong, because that is where the retryable/permanent
split has to be right or documents get stuck.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from core import repository
from core.models import Document, DocumentEvent, Extraction, Job, OcrResult
from core.status import DocStatus, JobKind, JobStatus
from doctypes import registry
from llm.base import ExtractionError, ExtractionRefused
from worker import run as worker

from .conftest import TINY_PNG, FakeExtractor, demo_org_id, receipt_definition, sqlite_only

OCR_TEXT = "CAFE LUNA\nTOTAL\n9.35"


@pytest.fixture(autouse=True)
def _cold_registry():
    registry.clear_cache()
    yield
    registry.clear_cache()


@pytest.fixture
def ready(session):
    """A document already through OCR, with an extract job waiting."""

    def _make(text: str = OCR_TEXT, *, with_ocr: bool = True):
        document, _ = repository.ingest(
            session,
            organisation_id=demo_org_id(session),
            data=TINY_PNG, mime_type="image/png", original_filename="a.png"
        )
        document.status = DocStatus.OCR_DONE
        if with_ocr:
            session.add(
                OcrResult(
                    document_id=document.id,
                    engine="fake",
                    engine_version="1.0",
                    params={},
                    full_text=text,
                    layout_blocks=[],
                    mean_confidence=0.99,
                )
            )
        job = Job(kind=JobKind.EXTRACT, document_id=document.id)
        session.add(job)
        session.commit()
        return document, job

    return _make


def claim_extract(session) -> Job:
    """Claim the queued extract job the way the worker would."""
    while (job := worker.claim(session, me="test")) is not None:
        if job.kind == JobKind.EXTRACT:
            return job
    raise AssertionError("no extract job queued")


class TestPermanentFailures:
    def test_a_refusal_sends_the_document_to_review_and_fails_the_job(
        self, session, ready
    ):
        repository.create_document_type_version(session, receipt_definition())
        document, _ = ready()
        job = claim_extract(session)

        worker.run_extract_job(
            session, job, FakeExtractor(raises=ExtractionRefused("model declined"))
        )

        session.refresh(document)
        assert document.status == DocStatus.NEEDS_REVIEW
        assert job.status == JobStatus.FAILED
        # Permanent means permanent: not requeued for another paid attempt.
        assert job.run_after is not None
        assert "declined" in job.last_error
        assert session.query(Extraction).count() == 0

    def test_the_review_event_says_why(self, session, ready):
        repository.create_document_type_version(session, receipt_definition())
        document, _ = ready()
        worker.run_extract_job(
            session,
            claim_extract(session),
            FakeExtractor(raises=ExtractionRefused("output hit max_tokens")),
        )

        session.refresh(document)
        last = document.events[-1]
        assert last.to_status == DocStatus.NEEDS_REVIEW
        assert last.detail["kind"] == "refused"

    def test_a_missing_ocr_result_does_not_crash_the_worker(self, session, ready):
        repository.create_document_type_version(session, receipt_definition())
        document, _ = ready(with_ocr=False)
        job = claim_extract(session)

        worker.run_extract_job(session, job, FakeExtractor())

        session.refresh(document)
        assert document.status == DocStatus.NEEDS_REVIEW
        assert job.status == JobStatus.FAILED
        assert "no OCR result" in job.last_error

    @sqlite_only
    def test_a_vanished_document_fails_the_job_without_a_transition(self, session, ready):
        """
        Same defensive guard as run_ocr_job, and the same proof that it cannot
        happen in practice: constructing the orphan needs the foreign key
        pragma switched off. Kept because SQLite disables foreign keys by
        default, so a database built without the engine factory would allow it.
        """
        from sqlalchemy import text

        ready()
        job = claim_extract(session)

        session.execute(text("PRAGMA foreign_keys=OFF"))
        session.query(OcrResult).delete()
        session.query(DocumentEvent).delete()
        session.query(Document).delete()
        session.commit()
        session.execute(text("PRAGMA foreign_keys=ON"))

        worker.run_extract_job(session, job, FakeExtractor())

        assert job.status == JobStatus.FAILED
        assert job.last_error == "document row is gone"


class TestTransientFailures:
    def test_a_transient_error_requeues_with_backoff(self, session, ready):
        repository.create_document_type_version(session, receipt_definition())
        document, _ = ready()
        job = claim_extract(session)

        worker.run_extract_job(
            session, job, FakeExtractor(raises=ExtractionError("rate limited"))
        )

        assert job.status == JobStatus.QUEUED
        assert job.locked_by is None
        assert job.attempts == 1
        assert "rate limited" in job.last_error

    def test_retries_are_exhausted_into_review_not_retried_forever(self, session, ready):
        repository.create_document_type_version(session, receipt_definition())
        document, _ = ready()
        extractor = FakeExtractor(raises=ExtractionError("connection failed"))

        job = claim_extract(session)
        for _ in range(10):
            worker.run_extract_job(session, job, extractor)
            if job.status == JobStatus.FAILED:
                break
            # The backoff would have elapsed before the worker looked again.
            job.run_after = worker.utcnow()
            session.commit()
            job = claim_extract(session)

        session.refresh(document)
        extract_job = session.query(Job).filter_by(kind=JobKind.EXTRACT).one()
        assert extract_job.status == JobStatus.FAILED
        assert extract_job.attempts == extract_job.max_attempts
        assert document.status == DocStatus.NEEDS_REVIEW
        # Every attempt was a real paid call, which is why max_attempts exists.
        assert len(extractor.calls) == extract_job.max_attempts


class TestRouting:
    def test_no_match_is_unsupported_and_never_calls_the_model(self, session, ready):
        repository.create_document_type_version(
            session,
            receipt_definition(matches={"keywords": ["albaran"], "min_score": 0.9}),
        )
        document, _ = ready()
        job = claim_extract(session)
        extractor = FakeExtractor()

        worker.run_extract_job(session, job, extractor)

        session.refresh(document)
        assert document.status == DocStatus.UNSUPPORTED
        assert job.status == JobStatus.DONE  # the job worked; the document is just unknown
        assert extractor.calls == []

    def test_an_unpublished_type_does_not_catch_traffic(self, session, ready):
        repository.create_document_type_version(
            session, receipt_definition(), publish=False, activate=False
        )
        document, _ = ready()

        worker.run_extract_job(session, claim_extract(session), FakeExtractor())

        session.refresh(document)
        assert document.status == DocStatus.UNSUPPORTED

    def test_the_issuer_profile_is_applied_and_recorded(self, session, ready):
        definition = receipt_definition(
            profiles={
                "luna": {
                    "name_patterns": ["CAFE LUNA"],
                    "descriptions": {"total": "The figure beneath the card line."},
                }
            }
        )
        repository.create_document_type_version(session, definition)
        document, _ = ready()
        extractor = FakeExtractor({"total": "9.35", "vendor": "CAFE LUNA"})

        worker.run_extract_job(session, claim_extract(session), extractor)

        # The override reached the model...
        sent = extractor.calls[0]["json_schema"]
        assert sent["properties"]["total"]["description"] == (
            "The figure beneath the card line."
        )
        # ...and which profile ran is on the row, which is how "did the new
        # hints help?" gets answered later.
        assert session.query(Extraction).one().profile == "luna"


class TestTheStoredRow:
    def test_cost_and_provenance_are_persisted(self, session, ready):
        repository.create_document_type_version(session, receipt_definition())
        document, _ = ready()

        worker.run_extract_job(
            session,
            claim_extract(session),
            FakeExtractor({"total": "9.35", "vendor": "CAFE LUNA"}),
        )

        row = session.query(Extraction).one()
        assert row.model_used == "claude-haiku-4-5-20251001"
        assert row.cost_usd == Decimal("0.001205")
        assert isinstance(row.cost_usd, Decimal)
        assert row.input_tokens == 790
        assert row.output_tokens == 83
        assert row.cache_read_tokens == 0
        assert row.latency_ms == 3370
        assert row.attempt == 1

    def test_raw_json_holds_what_the_model_said_not_the_coerced_values(
        self, session, ready
    ):
        """Decimal and date do not survive a JSON column, and evals score the raw answer."""
        repository.create_document_type_version(session, receipt_definition())
        document, _ = ready()

        worker.run_extract_job(
            session,
            claim_extract(session),
            FakeExtractor({"total": "9,35", "vendor": "CAFE LUNA"}),
        )

        row = session.query(Extraction).one()
        assert row.raw_json["total"] == "9,35"  # comma preserved, as printed
        assert row.validation_errors == []  # but it coerced and validated fine

    def test_a_second_extraction_supersedes_the_first(self, session, ready):
        repository.create_document_type_version(session, receipt_definition())
        document, _ = ready()

        worker.run_extract_job(
            session, claim_extract(session), FakeExtractor({"total": "9.35"})
        )
        session.add(Job(kind=JobKind.EXTRACT, document_id=document.id))
        session.commit()
        worker.run_extract_job(
            session, claim_extract(session), FakeExtractor({"total": "9.99"})
        )

        rows = session.query(Extraction).order_by(Extraction.attempt).all()
        assert len(rows) == 2
        assert [r.is_current for r in rows] == [False, True]
        assert rows[1].raw_json["total"] == "9.99"


class TestDispatch:
    def test_an_unknown_job_kind_still_fails_loudly(self, session, ready):
        document, _ = ready()
        session.add(Job(kind="teleport", document_id=document.id))
        session.commit()

        # Drain until the bogus one is claimed.
        for _ in range(5):
            job = worker.claim(session, me="test")
            if job is None:
                break
            if job.kind == "teleport":
                job.status = JobStatus.FAILED
                job.last_error = f"unknown job kind {job.kind!r}"
                session.commit()
                assert job.status == JobStatus.FAILED
                return
        raise AssertionError("the unknown job was never claimed")


CANONICAL = {
    "direction": "out",
    "amount": "total",
    "occurred_on": "issued_on",
    "counterparty": "vendor",
}


def _ledger_receipt():
    """The receipt type, plus the mapping that lets it reach the ledger."""
    return receipt_definition(
        fields={
            "total": {"type": "money", "description": "The total.", "required": True},
            "vendor": {"type": "string", "description": "The vendor."},
            "issued_on": {"type": "date", "description": "The date."},
        },
        canonical=CANONICAL,
    )


class TestReachingTheLedger:
    """
    `committed` was unreachable until the mapper existed: a document's best
    possible ending was `validated`, one step short of the thing the README
    opens by promising.
    """

    def test_a_validated_document_is_committed_to_the_ledger(self, session, ready):
        repository.create_document_type_version(session, _ledger_receipt())
        document, _ = ready()

        worker.run_extract_job(
            session,
            claim_extract(session),
            FakeExtractor({"total": "9,35", "vendor": "CAFE LUNA", "issued_on": "2026-05-12"}),
        )

        session.refresh(document)
        assert document.status == DocStatus.COMMITTED
        assert [e.to_status for e in document.events][-4:] == [
            "classified",
            "extracted",
            "validated",
            "committed",
        ]

    def test_the_row_is_typed_and_traceable(self, session, ready):
        from datetime import date
        from decimal import Decimal

        repository.create_document_type_version(session, _ledger_receipt())
        document, _ = ready()
        worker.run_extract_job(
            session,
            claim_extract(session),
            FakeExtractor({"total": "9,35", "vendor": "CAFE LUNA", "issued_on": "2026-05-12"}),
        )

        row = repository.current_transaction(session, document.id)
        assert row.amount == Decimal("9.3500")
        assert isinstance(row.amount, Decimal)
        assert row.occurred_on == date(2026, 5, 12)
        assert row.counterparty == "CAFE LUNA"
        assert row.direction == "out"
        assert row.source_type == "receipt"
        # Every figure traces back to the words on a page.
        assert row.extraction.raw_json["total"] == "9,35"

    def test_the_commit_event_records_what_was_written(self, session, ready):
        repository.create_document_type_version(session, _ledger_receipt())
        document, _ = ready()
        worker.run_extract_job(
            session, claim_extract(session), FakeExtractor({"total": "9,35"})
        )

        session.refresh(document)
        detail = document.events[-1].detail
        assert detail["amount"] == "9.35"
        assert detail["direction"] == "out"
        assert detail["transaction_id"]

    def test_the_event_and_the_column_agree(self, session, ready):
        """
        They did not, once. The column is Numeric(14, 4) and read back
        9.3500 while the audit note said 9.35 -- the same number, two
        spellings, and a trail that did not obviously match the table it
        described. core.money is now the single rule, applied on the way out
        of the database as well as into the event.
        """
        repository.create_document_type_version(session, _ledger_receipt())
        document, _ = ready()
        worker.run_extract_job(
            session, claim_extract(session), FakeExtractor({"total": "9,35"})
        )

        session.expire_all()  # force a real read back through the column
        row = repository.current_transaction(session, document.id)
        detail = session.get(type(document), document.id).events[-1].detail

        assert str(row.amount) == detail["amount"] == "9.35"

    def test_a_type_without_a_mapping_stops_at_validated(self, session, ready):
        """Not a failure: a type an analyst is still shaping has no ledger row."""
        repository.create_document_type_version(session, receipt_definition())
        document, _ = ready()

        worker.run_extract_job(
            session, claim_extract(session), FakeExtractor({"total": "9,35"})
        )

        session.refresh(document)
        assert document.status == DocStatus.VALIDATED
        assert repository.current_transaction(session, document.id) is None

    def test_a_document_in_review_never_reaches_the_ledger(self, session, ready):
        repository.create_document_type_version(session, _ledger_receipt())
        document, _ = ready()

        # Negative total trips the `positive` rule.
        worker.run_extract_job(
            session, claim_extract(session), FakeExtractor({"total": "-9,35"})
        )

        session.refresh(document)
        assert document.status == DocStatus.NEEDS_REVIEW
        assert repository.current_transaction(session, document.id) is None

    def test_re_extracting_supersedes_the_ledger_row_rather_than_adding_one(
        self, session, ready
    ):
        from decimal import Decimal

        from core.models import Transaction

        repository.create_document_type_version(session, _ledger_receipt())
        document, _ = ready()

        worker.run_extract_job(
            session, claim_extract(session), FakeExtractor({"total": "9,35"})
        )
        session.add(Job(kind=JobKind.EXTRACT, document_id=document.id))
        session.commit()
        worker.run_extract_job(
            session, claim_extract(session), FakeExtractor({"total": "9,99"})
        )

        live = session.query(Transaction).filter_by(is_current=True).all()
        assert len(live) == 1
        assert live[0].amount == Decimal("9.9900")
        # And the superseded row is kept, because the history is the point.
        assert session.query(Transaction).count() == 2


def _spent(cost="0.001900"):
    from decimal import Decimal

    from llm.base import ExtractionResult

    return ExtractionResult(
        data={},
        model="claude-haiku-4-5-20251001",
        input_tokens=1500,
        output_tokens=80,
        cost_usd=Decimal(cost),
        latency_ms=2900,
        stop_reason="refusal",
    )


class TestBilledFailuresAreRecorded:
    """
    A refused or truncated call is billed exactly like a success. Before this,
    it left no row, so every cost report quietly under-counted the calls that
    bought nothing -- the ones a cost report most needs to show.
    """

    def test_a_billed_refusal_leaves_a_row_with_its_cost(self, session, ready):
        from decimal import Decimal

        repository.create_document_type_version(session, receipt_definition())
        ready()

        worker.run_extract_job(
            session,
            claim_extract(session),
            FakeExtractor(raises=ExtractionRefused("model declined", spent=_spent())),
        )

        row = session.query(Extraction).one()
        assert row.cost_usd == Decimal("0.001900")
        assert row.input_tokens == 1500
        assert row.raw_json is None
        assert row.is_current is False
        assert "declined" in row.validation_errors[0]

    def test_an_unbilled_failure_still_leaves_nothing(self, session, ready):
        """A dropped connection never reached the model, so nothing was spent."""
        repository.create_document_type_version(session, receipt_definition())
        ready()

        worker.run_extract_job(
            session,
            claim_extract(session),
            FakeExtractor(raises=ExtractionError("connection failed")),
        )

        assert session.query(Extraction).count() == 0

    def test_a_billed_transient_failure_is_recorded_and_still_retried(
        self, session, ready
    ):
        repository.create_document_type_version(session, receipt_definition())
        ready()
        job = claim_extract(session)

        worker.run_extract_job(
            session,
            job,
            FakeExtractor(raises=ExtractionError("not valid JSON", spent=_spent())),
        )

        assert job.status == JobStatus.QUEUED
        assert session.query(Extraction).count() == 1

    def test_a_failed_attempt_never_replaces_a_good_answer(self, session, ready):
        """
        The property that makes recording failures safe: the row counts for
        cost, but a document's live extraction stays the one that worked.
        """
        repository.create_document_type_version(session, receipt_definition())
        document, _ = ready()

        worker.run_extract_job(
            session, claim_extract(session), FakeExtractor({"total": "9,35"})
        )
        session.add(Job(kind=JobKind.EXTRACT, document_id=document.id))
        session.commit()
        worker.run_extract_job(
            session,
            claim_extract(session),
            FakeExtractor(raises=ExtractionRefused("declined", spent=_spent())),
        )

        rows = session.query(Extraction).order_by(Extraction.attempt).all()
        assert [r.is_current for r in rows] == [True, False]
        assert rows[0].raw_json == {"total": "9,35"}
        # Numbered as a real attempt, because it was one.
        assert [r.attempt for r in rows] == [1, 2]
