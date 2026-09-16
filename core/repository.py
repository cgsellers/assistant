"""
Database operations for documents.

Kept separate from the HTTP layer so the worker (which has no request context)
can call the same functions the API does.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from core import storage
from core.models import Document, DocumentEvent, Job
from core.status import DocStatus


def find_by_sha256(session: Session, digest: str) -> Document | None:
    return session.scalar(select(Document).where(Document.sha256 == digest))


def get_document(session: Session, document_id: str) -> Document | None:
    return session.get(Document, document_id)


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
    session.add(Job(kind="ocr", document_id=document.id))

    session.commit()
    return document, True
