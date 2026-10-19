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
from decimal import Decimal

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    Numeric,
    String,
    Text,
    TypeDecorator,
    UniqueConstraint,
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


class UTCDateTime(TypeDecorator):
    """
    A DateTime that always round-trips as timezone-aware UTC.

    SQLite has no datetime type: it stores whatever string it is handed and
    returns it naive, dropping the offset. That breaks two things at once --
    comparing the result to an aware datetime raises TypeError, and letting
    SQLAlchemy send an aware value makes SQLite do a *string* comparison
    against naive rows.

    So: convert to UTC on the way in, tag as UTC on the way out. Postgres has
    a real timestamptz, so there we leave the value aware and let the database
    do the work.
    """

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect) -> datetime | None:
        if value is None:
            return None
        # A naive value here is assumed UTC rather than rejected -- utcnow()
        # always returns aware, so this only catches values from elsewhere.
        value = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
        return value.replace(tzinfo=None) if dialect.name == "sqlite" else value

    def process_result_value(self, value: datetime | None, dialect) -> datetime | None:
        if value is None:
            return None
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


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
    identical bytes returns this same row and queues no further work."""

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
        UTCDateTime, default=utcnow
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
        UTCDateTime, default=utcnow
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
        UTCDateTime, default=utcnow
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
        UTCDateTime, default=utcnow
    )
    locked_by: Mapped[str | None] = mapped_column(String(100))  # worker id
    locked_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime, default=utcnow
    )


# Serves the worker's poll query: "queued jobs whose run_after has passed".
Index("ix_jobs_poll", Job.status, Job.run_after)


# ---------------------------------------------------------------------------
# Phase 2: the generic extraction core.
#
# Nothing here knows about receipts, invoices or orders. A document type is
# data (`document_types`), and the result of applying one is JSON
# (`extractions.raw_json`). Domain-specific typed tables -- receipts,
# line_items, orders -- belong to a vertical built on top of this, and are an
# optimisation for fields that code must act on rather than a requirement.
# ---------------------------------------------------------------------------


class DocumentTypeDefinition(Base):
    """
    A document type, stored as data rather than code.

    Holds the field set with its per-field extraction descriptions, the
    deterministic matchers that route to it, the validation rules, and any
    per-issuer description overrides. One generic processor interprets all of
    it, which is what lets an analyst create a new parser through the UI
    without a deploy.

    Versioned and append-only: editing a type writes a new version so that
    extractions produced by the old one keep their meaning. `is_active` marks
    the version new documents are routed to.
    """

    __tablename__ = "document_types"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    #: Stable identifier stored on extractions.doc_type. Never rename it.
    name: Mapped[str] = mapped_column(String(50), index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)

    display_name: Mapped[str | None] = mapped_column(String(200))
    description: Mapped[str | None] = mapped_column(Text)

    # The whole definition: {fields: {...}, matches: {...}, rules: [...],
    # profiles: {...}}. One column rather than several tables because the UI
    # edits and versions it as a single document.
    definition: Mapped[dict] = mapped_column(JSON)

    #: Only one version per name should be active at a time.
    is_active: Mapped[bool] = mapped_column(Boolean, default=False)
    #: False for types an analyst is still authoring against sample documents.
    is_published: Mapped[bool] = mapped_column(Boolean, default=False)

    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime, default=utcnow, onupdate=utcnow
    )

    __table_args__ = (
        UniqueConstraint("name", "version"),
        Index("ix_document_types_active", "name", "is_active"),
    )


class Extraction(Base):
    """
    One attempt at turning OCR text into structured JSON.

    Append-only: a retry, a stronger model, or an edited document type writes a
    new row rather than overwriting, so the attempt history and its cost stay
    auditable and the authoring loop can diff versions. `is_current` marks the
    one that counts.

    `raw_json` is the universal typed layer. A document type with fifty
    optional fields needs no columns -- the JSON is queryable directly, and a
    field is promoted to a real column only when code must act on it.
    """

    __tablename__ = "extractions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    document_id: Mapped[str] = mapped_column(ForeignKey("documents.id"), index=True)

    #: document_types.name, plus the version that produced this row.
    doc_type: Mapped[str] = mapped_column(String(50))
    schema_version: Mapped[int] = mapped_column(Integer)
    #: Which per-issuer description profile was applied, if any. Without this,
    #: "did the new supplier hints help?" is unanswerable.
    profile: Mapped[str | None] = mapped_column(String(100))

    attempt: Mapped[int] = mapped_column(Integer, default=1)
    model_used: Mapped[str] = mapped_column(String(100))

    raw_json: Mapped[dict | None] = mapped_column(JSON)
    #: Empty list means the deterministic rules passed. Null means they have
    #: not run yet.
    validation_errors: Mapped[list | None] = mapped_column(JSON)
    is_current: Mapped[bool] = mapped_column(Boolean, default=True, index=True)

    #: True for an authoring dry run -- extracted to show the analyst, never
    #: committed to a typed table or counted as the document's result.
    is_trial: Mapped[bool] = mapped_column(Boolean, default=False)

    # Per-call cost tracking, so cost-per-document and "% handled per tier" are
    # reportable numbers rather than estimates. Numeric(12, 6) because a single
    # extraction costs a fraction of a cent and rounding to 2dp would erase it.
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    cache_read_tokens: Mapped[int | None] = mapped_column(Integer)
    cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    latency_ms: Mapped[int | None] = mapped_column(Integer)

    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    document: Mapped[Document] = relationship()


# The extractor's lookup: "the current, non-trial extraction for this document".
Index("ix_extractions_current", Extraction.document_id, Extraction.is_current)
