"""
The job worker.

Polls the `jobs` table, claims one row at a time, and runs whichever stage
the job names. No broker: the database is the queue. That is one less service
to run, and the claim is transactional, which a Redis list is not.

Two stages exist. `ocr` turns bytes into text. `extract` routes that text to a
document type, extracts against the type's schema and validates the result.

Run it with:  uv run python -m worker.run
"""

from __future__ import annotations

import argparse
import logging
import os
import socket
import time
from datetime import timedelta
from functools import lru_cache

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from core import money, repository, storage
from core.db import SessionLocal
from core.models import Document, DocumentEvent, Job, OcrResult, utcnow
from core.scoping import Scope, set_scope
from core.status import DocStatus, JobKind, JobStatus
from doctypes import listing, registry
from doctypes.canonical import derived_columns, to_ledger
from doctypes.definition import Definition
from doctypes.schema import build_json_schema
from doctypes.transform import Processed, process
from llm import get_extractor
from llm.base import ExtractionError, ExtractionRefused, Extractor
from ocr import get_engine
from ocr.base import OcrEngine, OcrError, UnsupportedMediaType
from ocr.layout import blocks_from_json, extraction_text

log = logging.getLogger("worker")

POLL_SECONDS = 2.0
MIGRATION_POLL_SECONDS = 5.0
# A job locked longer than this is presumed abandoned by a dead worker.
STALE_AFTER = timedelta(minutes=15)
# Retry backoff: attempt 1 waits 4s, attempt 2 waits 16s, attempt 3 waits 64s.
BACKOFF_BASE_SECONDS = 4


def worker_id() -> str:
    """
    Identifies who holds a lock, so a stale one can be attributed.

    Cut to jobs.locked_by's 100 characters: a fully qualified hostname can
    run past that, and on Postgres an over-long value fails the claim -- on
    every poll, so the worker would never take a job at all.
    """
    pid = f":{os.getpid()}"
    return socket.gethostname()[: 100 - len(pid)] + pid


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
    ready = (
        select(Job.id)
        .where(Job.status == JobStatus.QUEUED, Job.run_after <= now)
        .order_by(Job.run_after)
        .limit(1)
    )
    claim = update(Job).values(
        status=JobStatus.RUNNING,
        locked_by=me,
        locked_at=now,
        attempts=Job.attempts + 1,
    )

    if session.get_bind().dialect.name == "postgresql":
        # Lock the row while choosing it, and skip rows another worker has
        # locked: ten workers polling at once each take a different job
        # instead of queueing behind one row lock. The UPDATE is in the same
        # transaction, so the row is never visible as unclaimed in between.
        claimed_id = session.scalar(ready.with_for_update(skip_locked=True))
        if claimed_id is not None:
            session.execute(claim.where(Job.id == claimed_id))
    else:
        claimed_id = session.scalar(
            claim.where(Job.id == ready.scalar_subquery()).returning(Job.id)
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
            page_sizes=output.as_page_sizes() or None,
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
    # Extraction is a separate job rather than a continuation of this one.
    # Queued in the same transaction as the result, so a document never has
    # recognised text without something scheduled to read it.
    session.add(Job(kind=JobKind.EXTRACT, document_id=document.id))

    job.status = JobStatus.DONE
    job.last_error = None
    session.commit()
    log.info(
        "ocr done document=%s engine=%s blocks=%d conf=%s",
        document.id[:8], output.engine, len(output.blocks), output.mean_confidence,
    )


def _fail_permanently(
    session: Session,
    job: Job,
    document: Document,
    reason: str,
    *,
    kind: str,
    status: str = DocStatus.OCR_FAILED,
) -> None:
    """
    Give up on this job and park the document somewhere a human can find it.

    `status` differs by stage: unreadable bytes end at `ocr_failed`, while a
    document that was read fine but could not be extracted goes to
    `needs_review`, because the thing to do about it is look at it.
    """
    job.status = JobStatus.FAILED
    job.last_error = reason
    _record_transition(session, document, status, {"reason": reason, "kind": kind})
    session.commit()
    log.warning(
        "%s failed permanently document=%s %s", job.kind, document.id[:8], reason
    )


def _fail_or_retry(
    session: Session,
    job: Job,
    document: Document,
    reason: str,
    *,
    status: str = DocStatus.OCR_FAILED,
) -> None:
    """Transient failure: back off and try again until max_attempts is spent."""
    if job.attempts >= job.max_attempts:
        _fail_permanently(session, job, document, reason, kind="exhausted", status=status)
        return

    delay = BACKOFF_BASE_SECONDS ** job.attempts
    job.status = JobStatus.QUEUED
    job.locked_by = None
    job.locked_at = None
    job.run_after = utcnow() + timedelta(seconds=delay)
    job.last_error = reason
    session.commit()
    log.warning(
        "%s attempt %d/%d failed document=%s retrying in %ds: %s",
        job.kind, job.attempts, job.max_attempts, document.id[:8], delay, reason,
    )


def run_extract_job(session: Session, job: Job, extractor: Extractor) -> None:
    """
    Route one document to a type, extract against its schema, validate.

    Reads stored OCR text rather than the original file, which is what makes
    `reprocessing` cheap later: a schema bump re-runs this stage alone, with
    no re-upload and no second trip through the container.
    """
    document = session.get(Document, job.document_id)
    if document is None:
        job.status = JobStatus.FAILED
        job.last_error = "document row is gone"
        session.commit()
        return

    ocr = repository.get_ocr_result(session, document.id)
    if ocr is None:
        # Only reachable if the row was deleted between the two jobs: this job
        # is queued by run_ocr_job after the result is written.
        _fail_permanently(
            session, job, document, "no OCR result to extract from",
            kind="missing_ocr", status=DocStatus.NEEDS_REVIEW,
        )
        return

    # Rows rebuilt from the stored boxes, not the engine's flattened text: on a
    # two-column receipt the flat order pairs names with the wrong prices.
    text = extraction_text(ocr.full_text or "", blocks_from_json(ocr.layout_blocks))
    # Among the processors this document's organisation may use (DESIGN §9.3).
    match = registry.route(session, text, document.organisation_id)
    evidence = registry.scores(session, text, document.organisation_id)
    if match is None:
        # Not a failure. No registered type cleared its own min_score, so there
        # is nothing to extract against. A model classifier for this case is
        # phase 4; until then the document stops here rather than being
        # extracted against a guess. The job did its work, so it is done.
        _record_transition(
            session, document, DocStatus.UNSUPPORTED,
            {"reason": "no document type matched", "chars": len(text), "scores": evidence},
        )
        job.status = JobStatus.DONE
        job.last_error = None
        session.commit()
        log.info("unsupported document=%s no type matched %d chars", document.id[:8], len(text))
        return

    definition = match.definition
    # Recorded before the model call, so a document that dies mid-extraction
    # still shows what it was routed to and how confidently. On a retry this
    # logs a second classified event, which is honest: routing did run twice.
    _record_transition(
        session, document, DocStatus.CLASSIFIED,
        {
            "doc_type": definition.name,
            "schema_version": definition.version,
            "score": match.score,
            "profile": match.profile_key,
            "scores": evidence,
        },
    )
    session.commit()

    schema = build_json_schema(definition, match.profile)
    try:
        result = extractor.extract(
            json_schema=schema, text=text, doc_type=definition.name
        )
    except ExtractionError as exc:
        # Recorded before deciding what happens next, because the call may
        # have been billed whatever the outcome. `spent` is None when nothing
        # reached the model, and then there is nothing to record.
        if exc.spent is not None:
            repository.record_failed_attempt(
                session,
                document_id=document.id,
                doc_type=definition.name,
                schema_version=definition.version,
                reason=str(exc),
                spent=exc.spent,
                profile=match.profile_key,
            )
        if isinstance(exc, ExtractionRefused):
            # Permanent for these bytes: a refusal or a truncation reproduces.
            _fail_permanently(
                session, job, document, str(exc),
                kind="refused", status=DocStatus.NEEDS_REVIEW,
            )
        else:
            _fail_or_retry(
                session, job, document, str(exc), status=DocStatus.NEEDS_REVIEW
            )
        return

    # raw_json stores what the model actually returned, not the coerced values:
    # Decimal and date do not survive a JSON column, and the raw answer is the
    # evidence an eval run has to score against. The ledger is built from the
    # coerced values -- after the type's transform, if it has one, which is
    # stored beside the raw answer rather than over it.
    processed = process(definition, result.data)
    errors = processed.errors

    extraction = repository.record_extraction(
        session,
        document_id=document.id,
        doc_type=definition.name,
        schema_version=definition.version,
        model_used=result.model,
        profile=match.profile_key,
        raw_json=result.data,
        validation_errors=errors,
        transformed_json=processed.transformed,
        derived_fields=processed.derived if definition.transform else None,
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
        cache_read_tokens=result.cache_read_tokens,
        cost_usd=result.cost_usd,
        latency_ms=result.latency_ms,
    )

    # What the documents list shows, from what was read: set now, whatever the
    # rules decide next, because the list matters most for the documents
    # they send to review.
    listing.summarise(document, definition, processed.data)

    _record_transition(
        session, document, DocStatus.EXTRACTED,
        {
            "model": result.model,
            "cost_usd": money.to_str(result.cost_usd),
            "input_tokens": result.input_tokens,
            "output_tokens": result.output_tokens,
            "latency_ms": result.latency_ms,
        },
    )
    # Two transitions rather than one: the state machine distinguishes "the
    # model answered" from "the answer survived the rules", and phase 4 slots
    # the vision escalation between them.
    if errors:
        _record_transition(
            session, document, DocStatus.NEEDS_REVIEW, {"validation_errors": errors}
        )
        job.status = JobStatus.DONE
        job.last_error = None
        session.commit()
        log.info(
            "extract done document=%s type=%s v%d errors=%d cost=$%s",
            document.id[:8], definition.name, definition.version, len(errors),
            result.cost_usd,
        )
        return

    _record_transition(session, document, DocStatus.VALIDATED, None)
    session.commit()

    _commit_to_ledger(session, document, definition, extraction, processed)

    job.status = JobStatus.DONE
    job.last_error = None
    session.commit()
    log.info(
        "extract done document=%s type=%s v%d status=%s cost=$%s",
        document.id[:8], definition.name, definition.version, document.status,
        result.cost_usd,
    )


def _commit_to_ledger(
    session: Session,
    document: Document,
    definition: Definition,
    extraction,
    processed: Processed,
) -> None:
    """
    Write the canonical row, or record why the document stops at `validated`.

    Not every validated document reaches the ledger, and that is not a
    failure: a document type with no `canonical` mapping is one an analyst is
    still shaping. Stopping with the reason in the audit trail is more useful
    than inventing a row or failing the job.
    """
    values, reason = to_ledger(definition, processed.data)
    if values is None:
        log.info("document=%s stays validated: %s", document.id[:8], reason)
        return
    values["derived_fields"] = derived_columns(definition, processed.data, processed.derived)

    transaction = repository.record_transaction(
        session, extraction=extraction, values=values
    )
    _record_transition(
        session,
        document,
        DocStatus.COMMITTED,
        {
            "transaction_id": transaction.id,
            "amount": money.to_str(transaction.amount),
            "currency": transaction.currency,
            "direction": transaction.direction,
            "derived": transaction.derived_fields,
        },
    )


@lru_cache(maxsize=1)
def _default_extractor() -> Extractor:
    """
    Built on first use, then kept. On demand, so a worker that only ever sees
    OCR jobs never constructs one and never needs an API key; kept, so the
    HTTP client inside it -- and its open connections to the API -- is reused
    across jobs instead of being rebuilt for every document.
    """
    return get_extractor()


def tick(engine: OcrEngine, extractor: Extractor | None = None) -> bool:
    """One unit of work. Returns True if a job was processed."""
    with SessionLocal() as session:
        # The worker processes every organisation's documents, and says so:
        # its sessions are the system's own, not any client's.
        set_scope(session, Scope.everyone("worker"))
        if (returned := reclaim_stale(session)):
            log.info("reclaimed %d stale job(s)", returned)

        job = claim(session, me=worker_id())
        if job is None:
            return False

        log.info("claimed job=%s kind=%s attempt=%d", job.id[:8], job.kind, job.attempts)
        if job.kind == JobKind.OCR:
            run_ocr_job(session, job, engine)
        elif job.kind == JobKind.EXTRACT:
            run_extract_job(session, job, extractor or _default_extractor())
        else:
            job.status = JobStatus.FAILED
            job.last_error = f"unknown job kind {job.kind!r}"
            session.commit()
        return True


def wait_for_migrations(poll_seconds: float = MIGRATION_POLL_SECONDS) -> None:
    """
    Take no job until the database is at the migration this code expects.

    A worker that starts before a deploy's migration has run would claim jobs
    against the old schema and fail them, using up their retries on a problem
    that fixes itself a minute later. Waiting costs nothing.
    """
    from core import migrations
    from core.db import engine as db_engine

    while (reason := migrations.problem(db_engine)) is not None:
        log.warning("waiting: %s", reason)
        time.sleep(poll_seconds)


def main() -> None:
    parser = argparse.ArgumentParser(description="Ledgerline job worker")
    parser.add_argument("--once", action="store_true", help="drain the queue and exit")
    parser.add_argument("--poll", type=float, default=POLL_SECONDS)
    parser.add_argument(
        "--engine", default=None, help="override the configured OCR engine"
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s %(message)s"
    )
    engine = get_engine(args.engine)
    log.info("worker %s starting with engine=%s", worker_id(), engine.name)
    wait_for_migrations()

    while True:
        worked = tick(engine)
        if not worked:
            if args.once:
                log.info("queue empty, exiting")
                return
            time.sleep(args.poll)


if __name__ == "__main__":
    main()
