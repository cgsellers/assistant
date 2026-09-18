"""Job claiming, retries, and crash recovery."""

from __future__ import annotations

from datetime import timedelta

from core import repository
from core.models import Document, DocumentEvent, Job, OcrResult, utcnow
from core.status import DocStatus, JobStatus
from ocr.base import UnsupportedMediaType
from worker import run as worker

from .conftest import TINY_PNG, FakeEngine


def _ingest(session, data=TINY_PNG, name="receipt.png"):
    document, _ = repository.ingest(
        session, data=data, mime_type="image/png", original_filename=name
    )
    return document


def _job_for(session, document) -> Job:
    return session.query(Job).filter_by(document_id=document.id).one()


class TestClaim:
    def test_returns_none_on_an_empty_queue(self, session):
        assert worker.claim(session, me="w1") is None

    def test_marks_the_job_running_and_records_the_owner(self, session):
        _ingest(session)
        job = worker.claim(session, me="worker-1")

        assert job is not None
        assert job.status == JobStatus.RUNNING
        assert job.locked_by == "worker-1"
        assert job.locked_at is not None
        assert job.attempts == 1  # incremented as part of the claim

    def test_a_second_claim_cannot_take_the_same_job(self, session):
        """The core guarantee: the UPDATE is the lock."""
        _ingest(session)

        first = worker.claim(session, me="worker-1")
        second = worker.claim(session, me="worker-2")

        assert first is not None
        assert second is None  # nothing left in 'queued'

    def test_two_workers_take_different_jobs(self, session):
        _ingest(session, data=TINY_PNG, name="a.png")
        _ingest(session, data=TINY_PNG + b"b", name="b.png")

        first = worker.claim(session, me="worker-1")
        second = worker.claim(session, me="worker-2")

        assert first is not None and second is not None
        assert first.id != second.id

    def test_does_not_claim_a_job_scheduled_for_the_future(self, session):
        """How backoff actually defers work."""
        document = _ingest(session)
        job = _job_for(session, document)
        job.run_after = utcnow() + timedelta(minutes=5)
        session.commit()

        assert worker.claim(session, me="w1") is None

    def test_claims_the_oldest_ready_job_first(self, session):
        older = _ingest(session, data=TINY_PNG, name="older.png")
        newer = _ingest(session, data=TINY_PNG + b"x", name="newer.png")

        _job_for(session, older).run_after = utcnow() - timedelta(minutes=10)
        _job_for(session, newer).run_after = utcnow() - timedelta(minutes=1)
        session.commit()

        claimed = worker.claim(session, me="w1")
        assert claimed.document_id == older.id


class TestReclaimStale:
    def test_returns_a_job_abandoned_by_a_dead_worker(self, session):
        _ingest(session)
        job = worker.claim(session, me="dead-worker")
        job.locked_at = utcnow() - timedelta(hours=1)
        session.commit()

        assert worker.reclaim_stale(session) == 1

        session.refresh(job)
        assert job.status == JobStatus.QUEUED
        assert job.locked_by is None
        assert job.locked_at is None

    def test_does_not_reset_attempts(self, session):
        """
        Otherwise a job that reliably kills its worker loops forever instead of
        exhausting max_attempts.
        """
        _ingest(session)
        job = worker.claim(session, me="dead")
        attempts_before = job.attempts
        job.locked_at = utcnow() - timedelta(hours=1)
        session.commit()

        worker.reclaim_stale(session)
        session.refresh(job)
        assert job.attempts == attempts_before

    def test_leaves_a_recently_claimed_job_alone(self, session):
        _ingest(session)
        job = worker.claim(session, me="busy-worker")

        assert worker.reclaim_stale(session) == 0
        session.refresh(job)
        assert job.status == JobStatus.RUNNING


class TestRunOcrJobSuccess:
    def test_writes_the_result_and_advances_the_document(self, session, fake_engine):
        document = _ingest(session)
        job = worker.claim(session, me="w1")

        worker.run_ocr_job(session, job, fake_engine)

        result = session.query(OcrResult).one()
        assert result.document_id == document.id
        assert result.engine == "fake"
        assert result.engine_version == "1.0"
        assert result.full_text == "TOTAL 9.35"
        assert result.mean_confidence == 0.9
        assert result.params == {"stub": True}

        session.refresh(document)
        assert document.status == DocStatus.OCR_DONE
        assert job.status == JobStatus.DONE
        assert job.last_error is None

    def test_logs_the_transition_with_detail(self, session, fake_engine):
        document = _ingest(session)
        worker.run_ocr_job(session, worker.claim(session, me="w1"), fake_engine)

        event = (
            session.query(DocumentEvent)
            .filter_by(document_id=document.id, to_status=DocStatus.OCR_DONE)
            .one()
        )
        assert event.from_status == DocStatus.UPLOADED
        assert event.detail["engine"] == "fake"
        assert event.detail["blocks"] == 1

    def test_passes_the_stored_bytes_to_the_engine(self, session, fake_engine):
        _ingest(session)
        worker.run_ocr_job(session, worker.claim(session, me="w1"), fake_engine)
        assert fake_engine.calls == [(len(TINY_PNG), "image/png")]


class TestRunOcrJobPermanentFailure:
    def test_unsupported_media_type_does_not_retry(self, session):
        """Bytes will not become readable on a second attempt."""
        document = _ingest(session)
        job = worker.claim(session, me="w1")
        engine = FakeEngine(raises=UnsupportedMediaType("cannot read application/pdf"))

        worker.run_ocr_job(session, job, engine)

        assert job.status == JobStatus.FAILED
        assert job.attempts == 1  # spent one, not all three
        assert "cannot read" in job.last_error

        session.refresh(document)
        assert document.status == DocStatus.OCR_FAILED

        event = (
            session.query(DocumentEvent)
            .filter_by(to_status=DocStatus.OCR_FAILED)
            .one()
        )
        assert event.detail["kind"] == "unsupported"

    def test_writes_no_ocr_result_on_failure(self, session):
        _ingest(session)
        engine = FakeEngine(raises=UnsupportedMediaType("nope"))
        worker.run_ocr_job(session, worker.claim(session, me="w1"), engine)
        assert session.query(OcrResult).count() == 0


class TestRunOcrJobRetries:
    def test_transient_failure_requeues_with_backoff(self, session, failing_engine):
        document = _ingest(session)
        job = worker.claim(session, me="w1")
        before = utcnow()

        worker.run_ocr_job(session, job, failing_engine)

        assert job.status == JobStatus.QUEUED
        assert job.attempts == 1
        assert job.locked_by is None
        assert "engine exploded" in job.last_error
        # attempt 1 -> 4 ** 1 seconds
        assert job.run_after >= before + timedelta(seconds=worker.BACKOFF_BASE_SECONDS)

        session.refresh(document)
        assert document.status == DocStatus.UPLOADED  # unchanged, still pending

    def test_backoff_grows_with_each_attempt(self, session, failing_engine):
        _ingest(session)
        delays = []
        for _ in range(2):
            job = session.query(Job).one()
            job.run_after = utcnow() - timedelta(seconds=1)  # make it claimable
            job.status = JobStatus.QUEUED
            session.commit()

            claimed = worker.claim(session, me="w1")
            start = utcnow()
            worker.run_ocr_job(session, claimed, failing_engine)
            delays.append((claimed.run_after - start).total_seconds())

        assert delays[1] > delays[0]  # 4s then 16s

    def test_gives_up_after_max_attempts(self, session, failing_engine):
        document = _ingest(session)
        job = session.query(Job).one()
        job.attempts = job.max_attempts - 1
        session.commit()

        claimed = worker.claim(session, me="w1")  # attempts now == max_attempts
        worker.run_ocr_job(session, claimed, failing_engine)

        assert claimed.status == JobStatus.FAILED
        session.refresh(document)
        assert document.status == DocStatus.OCR_FAILED

        event = session.query(DocumentEvent).filter_by(to_status=DocStatus.OCR_FAILED).one()
        assert event.detail["kind"] == "exhausted"


class TestRunOcrJobEdgeCases:
    def test_handles_a_missing_document_row(self, session, fake_engine):
        """
        The guard in run_ocr_job is defensive: the foreign key from jobs to
        documents makes this state impossible while enforcement is on. Proven
        by the fact that constructing it requires switching the pragma off.

        Worth keeping the branch anyway -- SQLite disables foreign keys by
        default, so any database created without our engine factory would allow
        exactly this orphan.
        """
        from sqlalchemy import text

        _ingest(session)
        job = worker.claim(session, me="w1")

        session.execute(text("PRAGMA foreign_keys=OFF"))
        session.query(DocumentEvent).delete()
        session.query(Document).delete()
        session.commit()
        session.execute(text("PRAGMA foreign_keys=ON"))

        worker.run_ocr_job(session, job, fake_engine)

        assert job.status == JobStatus.FAILED
        assert "document row is gone" in job.last_error

    def test_missing_file_on_disk_is_treated_as_transient(self, session, fake_engine):
        """
        An OSError from storage means the file could not be read now -- a
        mounted volume might come back -- so it retries rather than condemning
        the document.
        """
        document = _ingest(session)
        job = worker.claim(session, me="w1")
        document.storage_key = "raw/zz/does-not-exist"
        session.commit()

        worker.run_ocr_job(session, job, fake_engine)

        assert job.status == JobStatus.QUEUED
        assert "FileNotFoundError" in job.last_error


class TestTick:
    def test_processes_one_job_and_reports_whether_it_worked(
        self, session_factory, monkeypatch, fake_engine
    ):
        """tick() opens its own session, so the factory is swapped for the sandbox."""
        monkeypatch.setattr(worker, "SessionLocal", session_factory)

        with session_factory() as s:
            repository.ingest(
                s, data=TINY_PNG, mime_type="image/png", original_filename="a.png"
            )

        assert worker.tick(fake_engine) is True   # one job done
        assert worker.tick(fake_engine) is False  # queue now empty

        with session_factory() as s:
            assert s.query(OcrResult).count() == 1
            assert s.query(Job).one().status == JobStatus.DONE

    def test_rejects_an_unknown_job_kind(self, session_factory, monkeypatch, fake_engine):
        monkeypatch.setattr(worker, "SessionLocal", session_factory)

        with session_factory() as s:
            document, _ = repository.ingest(
                s, data=TINY_PNG, mime_type="image/png", original_filename="a.png"
            )
            s.query(Job).delete()
            s.add(Job(kind="teleport", document_id=document.id))
            s.commit()

        assert worker.tick(fake_engine) is True
        with session_factory() as s:
            job = s.query(Job).one()
            assert job.status == JobStatus.FAILED
            assert "teleport" in job.last_error


class TestWorkerIdentity:
    def test_includes_hostname_and_pid(self):
        """So a stale lock can be attributed to a specific dead process."""
        import os

        assert str(os.getpid()) in worker.worker_id()
        assert ":" in worker.worker_id()
