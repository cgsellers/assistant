"""
The job worker.

Polls the `jobs` table, claims one row at a time, runs OCR, writes the result.
No broker: the database is the queue. That is one less service to run, and the
claim is transactional, which a Redis list is not.

Run it with:  uv run python -m worker.run
"""

from __future__ import annotations

import argparse
import logging
import os
import socket
import time
from datetime import timedelta

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from core import storage
from core.db import SessionLocal
from core.models import Document, DocumentEvent, Job, OcrResult, utcnow
from core.status import DocStatus, JobStatus
from ocr.base import OcrEngine, OcrError, UnsupportedMediaType
from ocr.tesseract import TesseractEngine

log = logging.getLogger("worker")

POLL_SECONDS = 2.0
# A job locked longer than this is presumed abandoned by a dead worker.
STALE_AFTER = timedelta(minutes=15)
# Retry backoff: attempt 1 waits 4s, attempt 2 waits 16s, attempt 3 waits 64s.
BACKOFF_BASE_SECONDS = 4


def worker_id() -> str:
    """Identifies who holds a lock, so a stale one can be attributed."""
    return f"{socket.gethostname()}:{os.getpid()}"


def claim(session: Session, *, me: str) -> Job | None:
    """
    Take ownership of one queued job, atomically.

    The UPDATE is the lock. Whichever worker's statement lands first flips the
    row out of 'queued'; everyone else's WHERE clause no longer matches and
    they claim nothing. No SELECT-then-UPDATE race, because there is no gap
    between deciding and taking.

    On Postgres this becomes SELECT ... FOR UPDATE SKIP LOCKED so that several
    workers can claim different rows concurrently; SQLite serialises writers
    so the subquery is sufficient here.
    """
    now = utcnow()
    oldest_ready = (
        select(Job.id)
        .where(Job.status == JobStatus.QUEUED, Job.run_after <= now)
        .order_by(Job.run_after)
        .limit(1)
        .scalar_subquery()
    )
    claimed_id = session.scalar(
        update(Job)
        .where(Job.id == oldest_ready)
        .values(
            status=JobStatus.RUNNING,
            locked_by=me,
            locked_at=now,
            attempts=Job.attempts + 1,
        )
        .returning(Job.id)
    )
    session.commit()

    return session.get(Job, claimed_id) if claimed_id else None


def reclaim_stale(session: Session) -> int:
    """
    Return jobs abandoned by dead workers to the queue.

    Without this a worker killed mid-job leaves its row in 'running' forever
    and the document silently never finishes. `attempts` is not reset, so a job
    that reliably kills its worker still exhausts max_attempts rather than
    looping indefinitely.
    """
    cutoff = utcnow() - STALE_AFTER
    result = session.execute(
        update(Job)
        .where(Job.status == JobStatus.RUNNING, Job.locked_at < cutoff)
        .values(status=JobStatus.QUEUED, locked_by=None, locked_at=None)
    )
    session.commit()
    return result.rowcount


def _record_transition(
    session: Session, document: Document, to_status: str, detail: dict | None = None
) -> None:
    session.add(
        DocumentEvent(
            document_id=document.id,
            from_status=document.status,
            to_status=to_status,
            detail=detail,
        )
    )
    document.status = to_status


def run_ocr_job(session: Session, job: Job, engine: OcrEngine) -> None:
    """Process one claimed OCR job. Commits its own outcome."""
    document = session.get(Document, job.document_id)
    if document is None:
        job.status = JobStatus.FAILED
        job.last_error = "document row is gone"
        session.commit()
        return

    try:
        data = storage.load(document.storage_key)
        output = engine.read(data, document.mime_type)

    except UnsupportedMediaType as exc:
        # Permanent: the bytes will not become readable on a retry.
        _fail_permanently(session, job, document, str(exc), kind="unsupported")
        return

    except (OcrError, OSError) as exc:
        _fail_or_retry(session, job, document, f"{type(exc).__name__}: {exc}")
        return

    session.add(
        OcrResult(
            document_id=document.id,
            engine=output.engine,
            engine_version=output.engine_version,
            params=output.params,
            full_text=output.full_text,
            layout_blocks=output.as_layout_blocks(),
            mean_confidence=output.mean_confidence,
        )
    )
    _record_transition(
        session,
        document,
        DocStatus.OCR_DONE,
        {
            "engine": output.engine,
            "blocks": len(output.blocks),
            "mean_confidence": output.mean_confidence,
            "chars": len(output.full_text),
        },
    )
    job.status = JobStatus.DONE
    job.last_error = None
    session.commit()
    log.info(
        "ocr done document=%s engine=%s blocks=%d conf=%s",
        document.id[:8], output.engine, len(output.blocks), output.mean_confidence,
    )


def _fail_permanently(
    session: Session, job: Job, document: Document, reason: str, *, kind: str
) -> None:
    job.status = JobStatus.FAILED
    job.last_error = reason
    _record_transition(session, document, DocStatus.OCR_FAILED, {"reason": reason, "kind": kind})
    session.commit()
    log.warning("ocr failed permanently document=%s %s", document.id[:8], reason)


def _fail_or_retry(session: Session, job: Job, document: Document, reason: str) -> None:
    """Transient failure: back off and try again until max_attempts is spent."""
    if job.attempts >= job.max_attempts:
        _fail_permanently(session, job, document, reason, kind="exhausted")
        return

    delay = BACKOFF_BASE_SECONDS ** job.attempts
    job.status = JobStatus.QUEUED
    job.locked_by = None
    job.locked_at = None
    job.run_after = utcnow() + timedelta(seconds=delay)
    job.last_error = reason
    session.commit()
    log.warning(
        "ocr attempt %d/%d failed document=%s retrying in %ds: %s",
        job.attempts, job.max_attempts, document.id[:8], delay, reason,
    )


def tick(engine: OcrEngine) -> bool:
    """One unit of work. Returns True if a job was processed."""
    with SessionLocal() as session:
        if (returned := reclaim_stale(session)):
            log.info("reclaimed %d stale job(s)", returned)

        job = claim(session, me=worker_id())
        if job is None:
            return False

        log.info("claimed job=%s kind=%s attempt=%d", job.id[:8], job.kind, job.attempts)
        if job.kind == "ocr":
            run_ocr_job(session, job, engine)
        else:
            job.status = JobStatus.FAILED
            job.last_error = f"unknown job kind {job.kind!r}"
            session.commit()
        return True


def main() -> None:
    parser = argparse.ArgumentParser(description="Ledgerline job worker")
    parser.add_argument("--once", action="store_true", help="drain the queue and exit")
    parser.add_argument("--poll", type=float, default=POLL_SECONDS)
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s %(message)s"
    )
    engine = TesseractEngine()
    log.info("worker %s starting with engine=%s", worker_id(), engine.name)

    while True:
        worked = tick(engine)
        if not worked:
            if args.once:
                log.info("queue empty, exiting")
                return
            time.sleep(args.poll)


if __name__ == "__main__":
    main()
