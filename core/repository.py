"""
Database operations for documents.

Kept separate from the HTTP layer so the worker (which has no request context)
can call the same functions the API does.
"""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from core import storage
from core.models import (
    Document,
    DocumentEvent,
    DocumentTypeDefinition,
    Extraction,
    Job,
    OcrResult,
    Transaction,
)
from core.status import DocStatus, JobKind
from doctypes.definition import Definition


def find_by_sha256(session: Session, digest: str) -> Document | None:
    return session.scalar(select(Document).where(Document.sha256 == digest))


def get_document(session: Session, document_id: str) -> Document | None:
    return session.get(Document, document_id)


def get_ocr_result(session: Session, document_id: str) -> OcrResult | None:
    return session.scalar(select(OcrResult).where(OcrResult.document_id == document_id))


def ingest(
    session: Session,
    *,
    data: bytes,
    mime_type: str,
    original_filename: str | None,
) -> tuple[Document, bool]:
    """
    Store a file and register it for processing.

    Returns (document, created). `created` is False when these exact bytes were
    already ingested -- the caller gets the original document back and nothing
    new is written. That makes upload safe to retry.
    """
    digest = storage.sha256_hex(data)

    existing = find_by_sha256(session, digest)
    if existing is not None:
        return existing, False

    # Write the file before the row. A file with no row is harmless garbage a
    # sweeper can collect; a row pointing at a missing file breaks the worker.
    key = storage.save(data, digest)

    document = Document(
        sha256=digest,
        storage_key=key,
        mime_type=mime_type,
        original_filename=original_filename,
        size_bytes=len(data),
        status=DocStatus.UPLOADED,
    )
    session.add(document)

    try:
        # flush() sends the INSERT now (so document.id exists for the rows
        # below) without ending the transaction.
        session.flush()
    except IntegrityError:
        # Two concurrent uploads of the same bytes: both missed the SELECT
        # above, both tried to INSERT, the unique index on sha256 rejected the
        # loser. The winner's row is what the caller wanted anyway.
        session.rollback()
        winner = find_by_sha256(session, digest)
        if winner is None:
            raise
        return winner, False

    session.add(
        DocumentEvent(
            document_id=document.id,
            from_status=None,
            to_status=DocStatus.UPLOADED,
            detail={"original_filename": original_filename, "size_bytes": len(data)},
        )
    )
    # The worker polls this table. Queuing inside the same transaction means a
    # document is never visible without its job, and never has a job without
    # being visible.
    session.add(Job(kind=JobKind.OCR, document_id=document.id))

    session.commit()
    return document, True


# --- Phase 2: document types and extractions --------------------------------


def get_active_document_type(session: Session, name: str) -> DocumentTypeDefinition | None:
    """
    The version of `name` that new documents route to, if any.

    Returns the row rather than a parsed Definition: callers that only need the
    id or the version number should not pay for validation, and the ones that
    do go through `doctypes.registry`.
    """
    return session.scalar(
        select(DocumentTypeDefinition).where(
            DocumentTypeDefinition.name == name,
            DocumentTypeDefinition.is_active.is_(True),
        )
    )


def create_document_type_version(
    session: Session,
    definition: Definition,
    *,
    publish: bool = True,
    activate: bool = True,
) -> DocumentTypeDefinition:
    """
    Write a new version of a document type.

    Takes a validated Definition rather than a dict so the JSON column cannot
    hold something that fails to parse on the way back out -- the failure mode
    the registry otherwise has to defend against on every document.

    Types are append-only: editing one never rewrites the row that produced
    existing extractions, because those rows' `schema_version` is the only
    record of what their fields were supposed to mean. The version number is
    therefore taken from the database (highest so far, plus one) and written
    back into the stored JSON, so the column and the document agree even when
    the caller hands over a draft carrying a stale number.

    `activate` moves the flag off whatever held it in the same transaction, so
    "exactly one active version per name" never has a window where it is false
    -- or where two versions are active and routing scores the type twice.
    """
    if activate and not publish:
        # Routing loads active AND published. An active unpublished version
        # would take the flag off a working type and then catch nothing: the
        # type silently stops routing. Refuse instead of producing that state.
        raise ValueError("cannot activate an unpublished version")

    highest = session.scalar(
        select(func.max(DocumentTypeDefinition.version)).where(
            DocumentTypeDefinition.name == definition.name
        )
    )
    version = (highest or 0) + 1
    stored = definition.model_copy(update={"version": version})

    if activate:
        session.execute(
            update(DocumentTypeDefinition)
            .where(
                DocumentTypeDefinition.name == definition.name,
                DocumentTypeDefinition.is_active.is_(True),
            )
            .values(is_active=False)
        )

    row = DocumentTypeDefinition(
        name=stored.name,
        version=version,
        display_name=stored.display_name,
        description=stored.description,
        definition=stored.model_dump(mode="json"),
        is_active=activate,
        is_published=publish,
    )
    session.add(row)
    session.commit()
    return row


def record_extraction(
    session: Session,
    *,
    document_id: str,
    doc_type: str,
    schema_version: int,
    model_used: str,
    profile: str | None = None,
    raw_json: dict | None = None,
    validation_errors: list | None = None,
    is_trial: bool = False,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    cache_read_tokens: int | None = None,
    cost_usd: Decimal | None = None,
    latency_ms: int | None = None,
) -> Extraction:
    """
    Append one extraction attempt.

    Nothing is overwritten: a retry, a stronger model or a new document type
    version adds a row, and `is_current` is what moves. Demoting the previous
    current row happens in the same transaction as the insert, so a reader
    never sees two current extractions for one document, and never sees zero.

    A trial is an authoring dry run shown to an analyst. It is written with
    `is_current` false and leaves the real current row alone -- otherwise
    previewing a draft type against a live document would silently replace that
    document's result.
    """
    if isinstance(cost_usd, float):
        # Decimal(0.1) is not 0.1, and these get summed into a cost report.
        raise TypeError("cost_usd must be a Decimal, not a float")

    # Attempts are numbered within their own class: a handful of trials against
    # a draft should not make the next real extraction look like attempt 9.
    highest = session.scalar(
        select(func.max(Extraction.attempt)).where(
            Extraction.document_id == document_id,
            Extraction.is_trial.is_(is_trial),
        )
    )

    if not is_trial:
        session.execute(
            update(Extraction)
            .where(
                Extraction.document_id == document_id,
                Extraction.is_current.is_(True),
            )
            .values(is_current=False)
        )

    extraction = Extraction(
        document_id=document_id,
        doc_type=doc_type,
        schema_version=schema_version,
        profile=profile,
        attempt=(highest or 0) + 1,
        model_used=model_used,
        raw_json=raw_json,
        validation_errors=validation_errors,
        is_current=not is_trial,
        is_trial=is_trial,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read_tokens,
        cost_usd=cost_usd,
        latency_ms=latency_ms,
    )
    session.add(extraction)
    session.commit()
    return extraction


def record_transaction(
    session: Session,
    *,
    extraction: Extraction,
    values: dict,
) -> Transaction:
    """
    Write a document's ledger row, superseding any earlier one.

    One live row per document, not per extraction. A retry or a correction
    produces a new extraction and therefore a new row; the old one is demoted
    in the same transaction rather than deleted, so the history survives and
    `SELECT SUM(amount) ... WHERE is_current` is the obvious correct query. A
    ledger that double-counts a re-run receipt is worse than one that is
    merely incomplete.
    """
    if isinstance(values.get("amount"), float):
        raise TypeError("amount must be a Decimal, not a float")

    session.execute(
        update(Transaction)
        .where(
            Transaction.document_id == extraction.document_id,
            Transaction.is_current.is_(True),
        )
        .values(is_current=False)
    )

    transaction = Transaction(
        source_extraction_id=extraction.id,
        document_id=extraction.document_id,
        **values,
    )
    session.add(transaction)
    session.commit()
    return transaction


def current_transaction(session: Session, document_id: str) -> Transaction | None:
    """The ledger row that counts for this document, if it has reached one."""
    return session.scalar(
        select(Transaction).where(
            Transaction.document_id == document_id,
            Transaction.is_current.is_(True),
        )
    )


def record_failed_attempt(
    session: Session,
    *,
    document_id: str,
    doc_type: str,
    schema_version: int,
    reason: str,
    spent,
    profile: str | None = None,
) -> Extraction:
    """
    Record a model call that was billed but produced nothing usable.

    A refusal or a max_tokens truncation is an HTTP 200 with a full usage
    block: it costs the same as a success. Without a row, that spend vanishes
    from every cost report -- and the calls that bought nothing are exactly
    the ones a cost report most needs to show.

    Written with `is_current=False` and no `raw_json`, so it is counted for
    cost and attempts but can never replace a document's real answer, and it
    does not demote whatever extraction is currently live.
    """
    highest = session.scalar(
        select(func.max(Extraction.attempt)).where(
            Extraction.document_id == document_id,
            Extraction.is_trial.is_(False),
        )
    )
    row = Extraction(
        document_id=document_id,
        doc_type=doc_type,
        schema_version=schema_version,
        profile=profile,
        attempt=(highest or 0) + 1,
        model_used=spent.model,
        raw_json=None,
        validation_errors=[f"failed: {reason}"],
        is_current=False,
        is_trial=False,
        input_tokens=spent.input_tokens,
        output_tokens=spent.output_tokens,
        cache_read_tokens=spent.cache_read_tokens,
        cost_usd=spent.cost_usd,
        latency_ms=spent.latency_ms,
    )
    session.add(row)
    session.commit()
    return row
