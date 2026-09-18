"""Ingest: dedup, the one-transaction write, and the race on identical bytes."""

from __future__ import annotations

import pytest
from sqlalchemy.exc import IntegrityError

from core import repository, storage
from core.models import Document, DocumentEvent, Job, OcrResult
from core.status import DocStatus, JobStatus

from .conftest import TINY_PNG


def _ingest(session, data=TINY_PNG, mime="image/png", name="receipt.png"):
    return repository.ingest(session, data=data, mime_type=mime, original_filename=name)


class TestIngest:
    def test_creates_document_event_and_job_together(self, session):
        document, created = _ingest(session)

        assert created is True
        assert document.status == DocStatus.UPLOADED
        assert document.sha256 == storage.sha256_hex(TINY_PNG)
        assert document.size_bytes == len(TINY_PNG)
        assert document.original_filename == "receipt.png"

        # All three rows, or none -- they share one transaction.
        assert session.query(Document).count() == 1
        assert session.query(DocumentEvent).count() == 1
        assert session.query(Job).count() == 1

    def test_writes_the_file_before_the_row(self, session, settings):
        document, _ = _ingest(session)
        assert storage.exists(document.storage_key)
        assert storage.load(document.storage_key) == TINY_PNG

    def test_queues_an_ocr_job_ready_to_run(self, session):
        document, _ = _ingest(session)
        job = session.query(Job).one()

        assert job.kind == "ocr"
        assert job.document_id == document.id
        assert job.status == JobStatus.QUEUED
        assert job.attempts == 0
        assert job.max_attempts == 3

    def test_first_event_records_the_upload(self, session):
        _ingest(session)
        event = session.query(DocumentEvent).one()

        assert event.from_status is None
        assert event.to_status == DocStatus.UPLOADED
        assert event.detail["original_filename"] == "receipt.png"


class TestDeduplication:
    def test_identical_bytes_return_the_original(self, session):
        first, created_first = _ingest(session)
        second, created_second = _ingest(session, name="a-different-name.png")

        assert created_first is True
        assert created_second is False
        assert second.id == first.id
        assert second.original_filename == "receipt.png"  # the original's name

    def test_duplicate_queues_no_extra_work(self, session):
        """The point of dedup: never pay to OCR the same bytes twice."""
        _ingest(session)
        _ingest(session)
        _ingest(session)

        assert session.query(Document).count() == 1
        assert session.query(Job).count() == 1
        assert session.query(DocumentEvent).count() == 1

    def test_different_bytes_are_separate_documents(self, session):
        first, _ = _ingest(session, data=TINY_PNG)
        second, created = _ingest(session, data=TINY_PNG + b"extra")

        assert created is True
        assert second.id != first.id
        assert session.query(Document).count() == 2

    def test_dedup_ignores_the_declared_mime_type(self, session):
        """Identity is the bytes. Nothing the caller says changes it."""
        first, _ = _ingest(session, mime="image/png")
        second, created = _ingest(session, mime="application/pdf")
        assert created is False
        assert second.id == first.id


class TestConcurrentIngest:
    def test_unique_index_rejects_a_second_row_for_the_same_bytes(self, session):
        """The database invariant the race handler depends on."""
        document, _ = _ingest(session)
        session.add(
            Document(
                sha256=document.sha256,  # same digest
                storage_key="raw/xx/other",
                mime_type="image/png",
                size_bytes=1,
                original_filename="clash.png",
            )
        )
        with pytest.raises(IntegrityError):
            session.commit()

    def test_losing_a_race_returns_the_winners_document(self, session, monkeypatch):
        """
        Simulates two workers passing the dedup check simultaneously.

        find_by_sha256 is stubbed to report 'not seen' on the first call, so
        ingest proceeds to INSERT and hits the unique index -- the real race's
        outcome. It must recover by returning the existing row, not raise.
        """
        existing, _ = _ingest(session)

        real = repository.find_by_sha256
        calls = {"n": 0}

        def flaky(sess, digest):
            calls["n"] += 1
            if calls["n"] == 1:
                return None  # pretend the other worker had not committed yet
            return real(sess, digest)

        monkeypatch.setattr(repository, "find_by_sha256", flaky)

        document, created = _ingest(session)

        assert created is False
        assert document.id == existing.id
        assert session.query(Document).count() == 1


class TestLookups:
    def test_get_document_returns_none_when_missing(self, session):
        assert repository.get_document(session, "nope") is None

    def test_get_ocr_result_returns_none_before_ocr_runs(self, session):
        document, _ = _ingest(session)
        assert repository.get_ocr_result(session, document.id) is None

    def test_get_ocr_result_returns_the_row_once_written(self, session):
        document, _ = _ingest(session)
        session.add(
            OcrResult(
                document_id=document.id,
                engine="fake",
                full_text="hello",
                layout_blocks=[],
                mean_confidence=0.9,
            )
        )
        session.commit()

        result = repository.get_ocr_result(session, document.id)
        assert result is not None
        assert result.full_text == "hello"
