"""
End to end: upload through the API, process with the worker, read the result
back through the API. Uses a fake engine and a fake extractor, so no
container, no tesseract, no network and no API key.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from core import repository
from core.models import Extraction, Job
from core.status import DocStatus, JobKind, JobStatus
from doctypes import registry
from ocr.base import OcrOutput, TextBlock, UnsupportedMediaType
from worker import run as worker

from .conftest import (
    TINY_PNG,
    FakeEngine,
    FakeExtractor,
    ForbiddenExtractor,
    receipt_definition,
)

RECEIPT = OcrOutput(
    full_text="CAFE LUNA\nTOTAL\n9.35",
    blocks=[
        TextBlock(text="CAFE LUNA", bbox=(20, 18, 120, 35), confidence=1.0),
        TextBlock(text="TOTAL", bbox=(20, 250, 80, 267), confidence=1.0),
        TextBlock(text="9.35", bbox=(300, 250, 360, 267), confidence=0.998),
    ],
    mean_confidence=0.999,
    engine="fake",
    engine_version="1.0",
    params={"dpi": 200},
)


@pytest.fixture(autouse=True)
def _cold_registry():
    """Parsed definitions outlive a tmp_path database, so start each test cold."""
    registry.clear_cache()
    yield
    registry.clear_cache()


@pytest.fixture
def run_worker(session_factory, monkeypatch):
    """
    tick() opens its own session; point it at the sandbox database.

    The extractor defaults to one that raises, so a test only reaches the
    model if it deliberately supplies a fake that answers.
    """
    monkeypatch.setattr(worker, "SessionLocal", session_factory)

    def _drain(engine, extractor=None) -> int:
        extractor = extractor or ForbiddenExtractor()
        processed = 0
        while worker.tick(engine, extractor):
            processed += 1
        return processed

    return _drain


@pytest.fixture
def registered_receipt(session):
    return repository.create_document_type_version(session, receipt_definition())


class TestHappyPath:
    def test_upload_process_and_read_back(
        self, client, run_worker, session, registered_receipt
    ):
        # 1. upload
        uploaded = client.post(
            "/documents", files={"file": ("cafe.png", TINY_PNG, "image/png")}
        ).json()
        assert uploaded["status"] == "uploaded"
        document_id = uploaded["id"]

        # 2. no text yet -- the worker has not run
        assert client.get(f"/documents/{document_id}/ocr").status_code == 404

        # 3. process: two stages, so two jobs
        extractor = FakeExtractor({"total": "9.35", "vendor": "CAFE LUNA"})
        assert run_worker(FakeEngine(RECEIPT), extractor) == 2

        # 4. all the way through the state machine
        detail = client.get(f"/documents/{document_id}").json()
        assert detail["status"] == DocStatus.VALIDATED
        assert [e["to_status"] for e in detail["events"]] == [
            "uploaded",
            "ocr_done",
            "classified",
            "extracted",
            "validated",
        ]

        # 5. text is readable
        ocr = client.get(f"/documents/{document_id}/ocr").json()
        assert ocr["full_text"] == "CAFE LUNA\nTOTAL\n9.35"
        assert ocr["mean_confidence"] == 0.999
        assert ocr["block_count"] == 3
        assert ocr["engine"] == "fake"

        # 6. boxes on request, with the amount separate from its label
        blocks = client.get(
            f"/documents/{document_id}/ocr", params={"include_blocks": True}
        ).json()["layout_blocks"]
        texts = {b["text"] for b in blocks}
        assert {"TOTAL", "9.35"} <= texts

        # 7. the extraction was stored, with what it cost
        extraction = session.query(Extraction).one()
        assert extraction.raw_json == {"total": "9.35", "vendor": "CAFE LUNA"}
        assert extraction.validation_errors == []
        assert extraction.doc_type == "receipt"
        assert extraction.schema_version == 1
        assert extraction.is_current is True
        assert extraction.is_trial is False
        assert extraction.cost_usd == Decimal("0.001205")
        assert extraction.input_tokens == 790
        assert extraction.output_tokens == 83

        # 8. the model saw the type's descriptions, not a generic prompt
        sent = extractor.calls[0]
        assert sent["doc_type"] == "receipt"
        assert sent["text"] == RECEIPT.full_text
        assert "labelled TOTAL" in sent["json_schema"]["properties"]["total"]["description"]

        # 9. queue drained
        assert {j.status for j in session.query(Job).all()} == {JobStatus.DONE}
        assert {j.kind for j in session.query(Job).all()} == {
            JobKind.OCR,
            JobKind.EXTRACT,
        }

    def test_failing_a_rule_routes_to_review_but_still_stores_the_answer(
        self, client, run_worker, session, registered_receipt
    ):
        """A bad value is kept, not discarded: review needs something to correct."""
        document_id = client.post(
            "/documents", files={"file": ("a.png", TINY_PNG, "image/png")}
        ).json()["id"]

        # Negative total trips the `positive` rule.
        run_worker(FakeEngine(RECEIPT), FakeExtractor({"total": "-9.35", "vendor": "X"}))

        detail = client.get(f"/documents/{document_id}").json()
        assert detail["status"] == DocStatus.NEEDS_REVIEW
        assert [e["to_status"] for e in detail["events"]][-2:] == [
            "extracted",
            "needs_review",
        ]

        extraction = session.query(Extraction).one()
        assert extraction.raw_json == {"total": "-9.35", "vendor": "X"}
        assert extraction.validation_errors
        assert "total" in extraction.validation_errors[0]


class TestNoTypeMatches:
    def test_a_document_no_type_claims_ends_unsupported(self, client, run_worker, session):
        """No document types registered, so nothing can claim this receipt."""
        document_id = client.post(
            "/documents", files={"file": ("a.png", TINY_PNG, "image/png")}
        ).json()["id"]

        # ForbiddenExtractor by default: routing must fail before any spend.
        assert run_worker(FakeEngine(RECEIPT)) == 2

        detail = client.get(f"/documents/{document_id}").json()
        assert detail["status"] == DocStatus.UNSUPPORTED
        assert detail["events"][-1]["detail"]["reason"] == "no document type matched"

        # The OCR text is still there -- this is recoverable by registering a
        # type and re-running, which is what `reprocessing` will do.
        assert client.get(f"/documents/{document_id}/ocr").status_code == 200
        assert session.query(Extraction).count() == 0

    def test_both_jobs_still_finish_cleanly(self, client, run_worker, session):
        client.post("/documents", files={"file": ("a.png", TINY_PNG, "image/png")})
        run_worker(FakeEngine(RECEIPT))

        # Unsupported is an outcome, not a failure: neither job is FAILED.
        assert {j.status for j in session.query(Job).all()} == {JobStatus.DONE}


class TestDuplicateCostsNothing:
    def test_reupload_queues_no_second_ocr_run(self, client, run_worker, session):
        """The economic point of dedup: no second OCR call and no second model call."""
        engine = FakeEngine(RECEIPT)

        first = client.post(
            "/documents", files={"file": ("a.png", TINY_PNG, "image/png")}
        ).json()
        assert run_worker(engine) == 2  # ocr, then extract

        second = client.post(
            "/documents", files={"file": ("a-again.png", TINY_PNG, "image/png")}
        ).json()
        assert second["id"] == first["id"]
        assert second["duplicate"] is True

        # Nothing new to do, and the engine was never called again.
        assert run_worker(engine) == 0
        assert len(engine.calls) == 1
        assert session.query(Job).count() == 2


class TestFailurePath:
    def test_an_unreadable_document_ends_at_ocr_failed(self, client, run_worker):
        uploaded = client.post(
            "/documents", files={"file": ("scan.pdf", b"%PDF-1.4" + b"\x00" * 64,
                                          "application/pdf")}
        ).json()

        engine = FakeEngine(raises=UnsupportedMediaType("tesseract cannot read PDF"))
        # One job only: OCR failing means no extract job is ever queued.
        assert run_worker(engine) == 1

        detail = client.get(f"/documents/{uploaded['id']}").json()
        assert detail["status"] == DocStatus.OCR_FAILED
        assert detail["events"][-1]["detail"]["kind"] == "unsupported"

        # The reason is retrievable, and there is no OCR result to read.
        assert client.get(f"/documents/{uploaded['id']}/ocr").status_code == 404

    def test_a_failure_does_not_block_the_rest_of_the_queue(
        self, client, run_worker, registered_receipt
    ):
        good = client.post(
            "/documents", files={"file": ("ok.png", TINY_PNG, "image/png")}
        ).json()
        bad = client.post(
            "/documents", files={"file": ("bad.png", TINY_PNG + b"x", "image/png")}
        ).json()

        class Selective(FakeEngine):
            def read(self, data, mime_type):
                self.calls.append((len(data), mime_type))
                if len(data) != len(TINY_PNG):
                    raise UnsupportedMediaType("cannot read this one")
                return RECEIPT

        # Three jobs: two OCR, plus the extract queued by the one that worked.
        extractor = FakeExtractor({"total": "9.35", "vendor": "CAFE LUNA"})
        assert run_worker(Selective(), extractor) == 3

        assert client.get(f"/documents/{good['id']}").json()["status"] == DocStatus.VALIDATED
        assert client.get(f"/documents/{bad['id']}").json()["status"] == DocStatus.OCR_FAILED


class TestAuditTrail:
    def test_every_transition_is_recorded_in_order(
        self, client, run_worker, registered_receipt
    ):
        document_id = client.post(
            "/documents", files={"file": ("a.png", TINY_PNG, "image/png")}
        ).json()["id"]
        run_worker(FakeEngine(RECEIPT), FakeExtractor({"total": "9.35", "vendor": "L"}))

        events = client.get(f"/documents/{document_id}").json()["events"]
        assert [(e["from_status"], e["to_status"]) for e in events] == [
            (None, "uploaded"),
            ("uploaded", "ocr_done"),
            ("ocr_done", "classified"),
            ("classified", "extracted"),
            ("extracted", "validated"),
        ]

        # The detail is enough to explain what each stage did.
        assert events[1]["detail"]["engine"] == "fake"
        assert events[1]["detail"]["blocks"] == 3
        assert events[1]["detail"]["chars"] == len(RECEIPT.full_text)

        assert events[2]["detail"]["doc_type"] == "receipt"
        assert events[2]["detail"]["schema_version"] == 1
        assert events[2]["detail"]["score"] > 0

        # Cost lands in the trail as well as the row, so "what did this
        # document cost?" is answerable from the audit log alone.
        assert events[3]["detail"]["cost_usd"] == "0.001205"
        assert events[3]["detail"]["input_tokens"] == 790
