"""
End to end: upload through the API, process with the worker, read the result
back through the API. Uses a fake engine, so no container and no tesseract.
"""

from __future__ import annotations

import pytest

from core.models import Job
from core.status import DocStatus, JobStatus
from ocr.base import OcrOutput, TextBlock, UnsupportedMediaType
from worker import run as worker

from .conftest import TINY_PNG, FakeEngine

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


@pytest.fixture
def run_worker(session_factory, monkeypatch):
    """tick() opens its own session; point it at the sandbox database."""
    monkeypatch.setattr(worker, "SessionLocal", session_factory)

    def _drain(engine) -> int:
        processed = 0
        while worker.tick(engine):
            processed += 1
        return processed

    return _drain


class TestHappyPath:
    def test_upload_process_and_read_back(self, client, run_worker, session):
        # 1. upload
        uploaded = client.post(
            "/documents", files={"file": ("cafe.png", TINY_PNG, "image/png")}
        ).json()
        assert uploaded["status"] == "uploaded"
        document_id = uploaded["id"]

        # 2. no text yet -- the worker has not run
        assert client.get(f"/documents/{document_id}/ocr").status_code == 404

        # 3. process
        assert run_worker(FakeEngine(RECEIPT)) == 1

        # 4. status advanced
        detail = client.get(f"/documents/{document_id}").json()
        assert detail["status"] == DocStatus.OCR_DONE
        assert [e["to_status"] for e in detail["events"]] == ["uploaded", "ocr_done"]

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

        # 7. queue drained
        assert session.query(Job).one().status == JobStatus.DONE


class TestDuplicateCostsNothing:
    def test_reupload_queues_no_second_ocr_run(self, client, run_worker, session):
        """The economic point of dedup: no second model or OCR call."""
        engine = FakeEngine(RECEIPT)

        first = client.post(
            "/documents", files={"file": ("a.png", TINY_PNG, "image/png")}
        ).json()
        assert run_worker(engine) == 1

        second = client.post(
            "/documents", files={"file": ("a-again.png", TINY_PNG, "image/png")}
        ).json()
        assert second["id"] == first["id"]
        assert second["duplicate"] is True

        # Nothing new to do, and the engine was never called again.
        assert run_worker(engine) == 0
        assert len(engine.calls) == 1
        assert session.query(Job).count() == 1


class TestFailurePath:
    def test_an_unreadable_document_ends_at_ocr_failed(self, client, run_worker):
        uploaded = client.post(
            "/documents", files={"file": ("scan.pdf", b"%PDF-1.4" + b"\x00" * 64,
                                          "application/pdf")}
        ).json()

        engine = FakeEngine(raises=UnsupportedMediaType("tesseract cannot read PDF"))
        assert run_worker(engine) == 1

        detail = client.get(f"/documents/{uploaded['id']}").json()
        assert detail["status"] == DocStatus.OCR_FAILED
        assert detail["events"][-1]["detail"]["kind"] == "unsupported"

        # The reason is retrievable, and there is no OCR result to read.
        assert client.get(f"/documents/{uploaded['id']}/ocr").status_code == 404

    def test_a_failure_does_not_block_the_rest_of_the_queue(self, client, run_worker):
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

        assert run_worker(Selective()) == 2

        assert client.get(f"/documents/{good['id']}").json()["status"] == DocStatus.OCR_DONE
        assert client.get(f"/documents/{bad['id']}").json()["status"] == DocStatus.OCR_FAILED


class TestAuditTrail:
    def test_every_transition_is_recorded_in_order(self, client, run_worker):
        document_id = client.post(
            "/documents", files={"file": ("a.png", TINY_PNG, "image/png")}
        ).json()["id"]
        run_worker(FakeEngine(RECEIPT))

        events = client.get(f"/documents/{document_id}").json()["events"]
        assert [(e["from_status"], e["to_status"]) for e in events] == [
            (None, "uploaded"),
            ("uploaded", "ocr_done"),
        ]
        # The detail is enough to explain what the engine did.
        assert events[1]["detail"]["engine"] == "fake"
        assert events[1]["detail"]["blocks"] == 3
        assert events[1]["detail"]["chars"] == len(RECEIPT.full_text)
