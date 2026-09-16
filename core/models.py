"""
Phase 1 tables: documents, ocr_results, document_events, jobs.

SQLAlchemy 2.0 declarative style. Each attribute is declared twice over:
`Mapped[...]` is the Python-side type (what your editor and type checker see),
`mapped_column(...)` is the database-side definition (column type, constraints).
A `Mapped[str | None]` annotation makes the column NULLable automatically.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from core.status import DocStatus, JobStatus

# Without this, SQLAlchemy lets the database invent names for indexes, unique
# constraints and foreign keys. Alembic then can't reliably drop or alter them
# later -- which matters here because SQLite has no real ALTER TABLE, so Alembic
# rebuilds the whole table ("batch mode") and has to name every constraint it
# recreates. Setting the convention now costs nothing; retrofitting it once
# migrations exist means hand-writing renames.
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


def new_id() -> str:
    """
    UUIDs stored as 36-char strings: SQLite has no UUID type and Postgres
    accepts the same text. Switching to a native `uuid` column on Postgres
    later is a single Alembic operation.
    """
    return str(uuid.uuid4())


def utcnow() -> datetime:
    """Timezone-aware UTC. Naive datetimes in a finance system are a bug waiting."""
    return datetime.now(UTC)


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class Document(Base):
    """One uploaded file. `sha256` is the dedup key: a repeat upload of
    identical bytes is marked `duplicate` rather than processed again."""

    __tablename__ = "documents"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    sha256: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    storage_key: Mapped[str] = mapped_column(String(255))
    mime_type: Mapped[str] = mapped_column(String(100))
    original_filename: Mapped[str | None] = mapped_column(String(255))
    size_bytes: Mapped[int] = mapped_column(Integer)
    # Plain String rather than SQLAlchemy's Enum type: Enum emits a CHECK
    # constraint, and this vocabulary grows every phase. DocStatus is the
    # source of truth in Python; the DB just stores the text.
    status: Mapped[str] = mapped_column(
        String(32), index=True, default=DocStatus.UPLOADED
    )
    uploaded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )

    ocr_result: Mapped[OcrResult | None] = relationship(
        back_populates="document", uselist=False
    )
    events: Mapped[list[DocumentEvent]] = relationship(
        back_populates="document", order_by="DocumentEvent.created_at"
    )


class OcrResult(Base):
    """Raw OCR output, kept verbatim so extraction can be re-run in phase 5
    without re-uploading or re-OCRing the file."""

    __tablename__ = "ocr_results"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    # One result per document in phase 1. Phase 3 compares engines on the same
    # document, which relaxes this to a unique (document_id, engine) pair.
    document_id: Mapped[str] = mapped_column(
        ForeignKey("documents.id"), unique=True
    )
    engine: Mapped[str] = mapped_column(String(50))  # "tesseract", "paddleocr", ...
    engine_version: Mapped[str | None] = mapped_column(String(100))
    # Engine-specific knobs (language, dpi, model size) so an eval run can be
    # reproduced exactly from the row.
    params: Mapped[dict | None] = mapped_column(JSON)
    full_text: Mapped[str] = mapped_column(Text)
    # [{"text": ..., "bbox": [x0, y0, x1, y1], "confidence": 0.98, "page": 1}, ...]
    # Bounding boxes are what let the analyst cite a spot on the page.
    layout_blocks: Mapped[list] = mapped_column(JSON)
    mean_confidence: Mapped[float | None] = mapped_column(Float)  # null for VLMs
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )

    document: Mapped[Document] = relationship(back_populates="ocr_result")


class DocumentEvent(Base):
    """Append-only audit trail. Every status change writes one row, so a
    document's whole history is reconstructable after the fact."""

    __tablename__ = "document_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    document_id: Mapped[str] = mapped_column(ForeignKey("documents.id"), index=True)
    from_status: Mapped[str | None] = mapped_column(String(32))  # null on creation
    to_status: Mapped[str] = mapped_column(String(32))
    detail: Mapped[dict | None] = mapped_column(JSON)  # error text, timings, ...
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )

    document: Mapped[Document] = relationship(back_populates="events")


class Job(Base):
    """
    DB-backed work queue -- no Redis, no broker to run.

    A worker claims a row with a conditional UPDATE against status='queued'.
    SQLite serialises writers, so that claim is safe as-is; on Postgres it
    becomes SELECT ... FOR UPDATE SKIP LOCKED to let workers run in parallel.

    `run_after` gives retries a backoff delay, and `locked_by`/`locked_at` let
    a supervisor reclaim jobs from a worker that died mid-run.
    """

    __tablename__ = "jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    kind: Mapped[str] = mapped_column(String(50))  # "ocr" is the only kind in phase 1
    document_id: Mapped[str] = mapped_column(ForeignKey("documents.id"), index=True)
    status: Mapped[str] = mapped_column(String(20), default=JobStatus.QUEUED)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)
    run_after: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
    locked_by: Mapped[str | None] = mapped_column(String(100))  # worker id
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )


# Serves the worker's poll query: "queued jobs whose run_after has passed".
Index("ix_jobs_poll", Job.status, Job.run_after)
