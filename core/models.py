"""
The tables: documents, OCR results, events and the job queue; document types,
extractions and the ledger; API keys, users and sessions.

SQLAlchemy 2.0 declarative style. Each attribute is declared twice over:
`Mapped[...]` is the Python-side type (what your editor and type checker see),
`mapped_column(...)` is the database-side definition (column type, constraints).
A `Mapped[str | None]` annotation makes the column NULLable automatically.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
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
    event,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, relationship

from core.money import normalise
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

    The column is `DateTime(timezone=True)` for that reason. It was a plain
    `DateTime` once, which made every Postgres column after the first
    migration a `timestamp without time zone` -- where an aware value is
    silently converted using the server's TimeZone setting, so what was
    stored depended on how the database happened to be configured.
    """

    impl = DateTime(timezone=True)
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


#: JSON columns: JSONB on Postgres, plain JSON elsewhere. JSONB is parsed once
#: on write, can be indexed and queried by key -- "every invoice with PO 4471"
#: is a query on raw_json -- and compares by value. SQLite has one JSON type,
#: so there it is the same column it always was. JSONB does not keep key
#: order, which is why a processor's definition is not one of these.
JsonType = JSON().with_variant(JSONB(), "postgresql")


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


class Money(TypeDecorator):
    """
    A Decimal that reads back the way it was written.

    `Numeric(14, 4)` is the right column -- two places would truncate the
    currencies that use three -- but it means a value written as `10.00`
    returns as `10.0000`. Identical numbers, two spellings, and everything
    downstream that renders one produces inconsistent output: an audit trail
    saying `9.35` beside a ledger row saying `9.3500`.

    Exactly the shape of problem `UTCDateTime` solves for datetimes (5.9), so
    exactly the same answer: normalise at the boundary, once, rather than
    asking every caller to remember. `core.money.normalise` holds the rule.

    No DDL change -- the column is still Numeric(14, 4), and `render_item` in
    alembic/env.py already emits the impl rather than this class.
    """

    impl = Numeric
    cache_ok = True

    def process_result_value(self, value, dialect):
        return None if value is None else normalise(value)


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class Organisation(Base):
    """
    A client company -- or the Ledgerline team's own, "Ledgerline demo".

    Tenancy (DESIGN §9.3): every document belongs to exactly one organisation,
    and so does everything made from it -- OCR, events, jobs, extractions,
    ledger rows. Users and API keys belong to one too. A key sees only its
    own organisation's documents; a user marked `is_staff` sees all of them,
    which is what the admin panel is for.
    """

    __tablename__ = "organisations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(200), unique=True)
    #: Short and stable, for URLs and the command line.
    slug: Mapped[str] = mapped_column(String(60), unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class Document(Base):
    """One uploaded file. `(organisation_id, sha256)` is the dedup key: a repeat
    upload of identical bytes by the same organisation returns this same row
    and queues no further work."""

    __tablename__ = "documents"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    #: Who this belongs to. See the tenancy note above Organisation.
    organisation_id: Mapped[str] = mapped_column(ForeignKey("organisations.id"), index=True)
    #: Not unique on its own any more. It was, and so a client uploading a file
    #: another client had already sent got the *other client's* document back,
    #: extracted data and all. Unique per organisation, below.
    sha256: Mapped[str] = mapped_column(String(64), index=True)
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

    __table_args__ = (UniqueConstraint("organisation_id", "sha256"),)


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
    #: The document's organisation, copied onto this row by the before_flush
    #: hook below rather than by each caller -- see `_follow_the_document`.
    organisation_id: Mapped[str] = mapped_column(ForeignKey("organisations.id"), index=True)
    engine: Mapped[str] = mapped_column(String(50))  # "tesseract", "paddleocr", ...
    engine_version: Mapped[str | None] = mapped_column(String(100))
    # Engine-specific knobs (language, dpi, model size) so an eval run can be
    # reproduced exactly from the row.
    params: Mapped[dict | None] = mapped_column(JsonType)
    full_text: Mapped[str] = mapped_column(Text)
    # [{"text": ..., "bbox": [x0, y0, x1, y1], "confidence": 0.98, "page": 1}, ...]
    # Bounding boxes are what let the analyst cite a spot on the page.
    layout_blocks: Mapped[list] = mapped_column(JsonType)
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
    #: The document's organisation, copied onto this row by the before_flush
    #: hook below rather than by each caller -- see `_follow_the_document`.
    organisation_id: Mapped[str] = mapped_column(ForeignKey("organisations.id"), index=True)
    from_status: Mapped[str | None] = mapped_column(String(32))  # null on creation
    to_status: Mapped[str] = mapped_column(String(32))
    detail: Mapped[dict | None] = mapped_column(JsonType)  # error text, timings, ...
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
    kind: Mapped[str] = mapped_column(String(50))  # a JobKind: "ocr" or "extract"
    document_id: Mapped[str] = mapped_column(ForeignKey("documents.id"), index=True)
    #: The document's organisation, copied onto this row by the before_flush
    #: hook below rather than by each caller -- see `_follow_the_document`.
    organisation_id: Mapped[str] = mapped_column(ForeignKey("organisations.id"), index=True)
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
    #
    # Plain JSON on Postgres too, never JSONB: JSONB sorts object keys, and
    # the order of `fields` is the author's -- it is the order the model is
    # shown them, the panel lists them and a draft label reads. Running the
    # suite on Postgres caught it reordering them. Nothing queries inside a
    # definition, so JSONB would buy nothing here.
    definition: Mapped[dict] = mapped_column(JSON)

    #: Only one version per name should be active at a time.
    is_active: Mapped[bool] = mapped_column(Boolean, default=False)
    #: False for types an analyst is still authoring against sample documents.
    is_published: Mapped[bool] = mapped_column(Boolean, default=False)

    #: Who wrote this version: "seed", "key:<name>", "user:<email>", with
    #: " (ai draft)" when a model drafted it. Wide enough for the longest legal
    #: email (254) plus both: 200 was not, and Postgres enforces the limit.
    created_by: Mapped[str | None] = mapped_column(String(320))
    #: The published version this one was edited from; null for a new type.
    #: Publishing refuses if a newer version has been published since, so two
    #: people editing the same type cannot silently overwrite each other.
    based_on_version: Mapped[int | None] = mapped_column(Integer)

    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime, default=utcnow, onupdate=utcnow
    )

    __table_args__ = (
        UniqueConstraint("name", "version"),
        Index("ix_document_types_active", "name", "is_active"),
        # The invariant routing depends on, held by the database rather than
        # only by the code that demotes before it promotes. Two active
        # versions would score the type twice; with several workers and
        # panel users, "the code always does it right" stops being enough.
        Index(
            "uq_document_types_one_active", "name", unique=True,
            sqlite_where=text("is_active"), postgresql_where=text("is_active"),
        ),
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
    #: The document's organisation, copied onto this row by the before_flush
    #: hook below rather than by each caller -- see `_follow_the_document`.
    organisation_id: Mapped[str] = mapped_column(ForeignKey("organisations.id"), index=True)

    #: document_types.name, plus the version that produced this row.
    doc_type: Mapped[str] = mapped_column(String(50))
    schema_version: Mapped[int] = mapped_column(Integer)
    #: Which per-issuer description profile was applied, if any. Without this,
    #: "did the new supplier hints help?" is unanswerable.
    profile: Mapped[str | None] = mapped_column(String(100))

    attempt: Mapped[int] = mapped_column(Integer, default=1)
    model_used: Mapped[str] = mapped_column(String(100))

    raw_json: Mapped[dict | None] = mapped_column(JsonType)
    #: Empty list means the deterministic rules passed. Null means they have
    #: not run yet.
    validation_errors: Mapped[list | None] = mapped_column(JsonType)
    #: What the document type's transform returned, if it has one and it ran.
    #: `raw_json` stays exactly what the model read; this is beside it.
    transformed_json: Mapped[dict | None] = mapped_column(JsonType)
    #: Fields whose value the transform changed or filled in -- not read off
    #: the page. Null when no transform ran.
    derived_fields: Mapped[list | None] = mapped_column(JsonType)
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
# At most one current extraction per document, enforced where it cannot be
# raced. Trials are never current, so they are outside it by construction.
Index(
    "uq_extractions_one_current", Extraction.document_id, unique=True,
    sqlite_where=text("is_current"), postgresql_where=text("is_current"),
)


class Transaction(Base):
    """
    The canonical ledger row: one real-world movement of money.

    The only table the analyst agent reads. Deliberately narrow -- a document
    type may collect fifty fields, and six of them matter to "what did we
    spend". Everything else stays in `extractions.raw_json`, which is what the
    raw layer is for. A field is promoted here when code must act on it.

    One row per document, not per extraction. A retry or a correction writes a
    new row and demotes the old one, the same way extractions work: a ledger
    that double-counts a re-run receipt is worse than useless, and summing
    `WHERE is_current` has to be the obvious correct query.
    """

    __tablename__ = "transactions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)

    #: Provenance, and the citation the analyst agent answers with. Every
    #: figure in the ledger can be traced back to the words on a page.
    source_extraction_id: Mapped[str] = mapped_column(
        ForeignKey("extractions.id"), index=True
    )
    #: Denormalised from the extraction: the supersede query and every
    #: "show me this document's ledger entry" lookup go through it.
    document_id: Mapped[str] = mapped_column(ForeignKey("documents.id"), index=True)
    #: The document's organisation, copied onto this row by the before_flush
    #: hook below rather than by each caller -- see `_follow_the_document`.
    organisation_id: Mapped[str] = mapped_column(ForeignKey("organisations.id"), index=True)
    #: The document type that produced this, e.g. "receipt".
    source_type: Mapped[str] = mapped_column(String(50))

    # A plain Date, not a timestamp. A purchase happened on a day; the hour
    # a till roll was printed is not ledger information, and storing it as a
    # datetime would invite timezone questions that have no answer here.
    occurred_on: Mapped[date | None] = mapped_column(Date)
    counterparty: Mapped[str | None] = mapped_column(String(200))
    counterparty_tax_id: Mapped[str | None] = mapped_column(String(50))
    #: "out" for money leaving, "in" for money arriving. A property of the
    #: document type rather than the document -- see CanonicalMap.
    direction: Mapped[str] = mapped_column(String(10))

    # Numeric(14, 4) rather than (14, 2): the wire format caps at two decimal
    # places today, but that is a property of the money pattern in
    # doctypes.schema rather than of money, and currencies with three exist.
    # Four costs nothing and removes a silent rounding trap.
    amount: Mapped[Decimal] = mapped_column(Money(14, 4))
    tax_amount: Mapped[Decimal | None] = mapped_column(Money(14, 4))
    #: Null when the document does not say. Not guessed: a receipt printed in
    #: Spain is usually EUR and "usually" has no place in a ledger.
    currency: Mapped[str | None] = mapped_column(String(3))
    #: Ledger columns whose value a transform worked out rather than the
    #: model reading it off the page, e.g. ["amount"] for a computed total.
    #: Null on rows written before transforms existed.
    derived_fields: Mapped[list | None] = mapped_column(JsonType, default=list)

    #: Assigned later, by rules or by a human. Categorisation is not
    #: extraction and does not belong in the same step.
    category: Mapped[str | None] = mapped_column(String(100))
    #: True once a human has confirmed the row, which is what lets a report
    #: distinguish "the model said so" from "somebody checked".
    user_verified: Mapped[bool] = mapped_column(Boolean, default=False)

    is_current: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    extraction: Mapped[Extraction] = relationship()
    document: Mapped[Document] = relationship()


# The ledger's own lookup: "this document's live entry", and the join behind
# every "what did we spend" query.
Index("ix_transactions_current", Transaction.document_id, Transaction.is_current)
# One live ledger row per document. Two would count a receipt twice -- the
# failure the ledger is most careful about -- so the database refuses it even
# if two workers race the demote-then-insert in record_transaction.
Index(
    "uq_transactions_one_current", Transaction.document_id, unique=True,
    sqlite_where=text("is_current"), postgresql_where=text("is_current"),
)


class ApiKey(Base):
    """
    A credential allowed to call the ingestion API.

    The key itself is never stored -- only its SHA-256 and enough of a prefix
    to tell two apart in a listing. A database dump therefore leaks no access.
    See core/auth.py for why a fast hash is the right choice for a
    high-entropy secret and the wrong one for a password.

    There is no owner column here or anywhere else: a key that gets in sees
    everything. This is a locked front door, not per-caller access control,
    and the schema would need tenancy before it could be anything more.
    """

    __tablename__ = "api_keys"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    #: What this key is for, so an unused one can be retired with confidence.
    name: Mapped[str] = mapped_column(String(200))
    #: The organisation whose documents this key may see -- only those. Keys
    #: are never staff: they are what end up inside ERPs and scripts.
    organisation_id: Mapped[str] = mapped_column(ForeignKey("organisations.id"), index=True)
    key_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    #: The first few characters, shown in listings and logs. Not enough to use.
    prefix: Mapped[str] = mapped_column(String(20), index=True)

    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    #: Updated at most once a minute -- see TOUCH_AFTER. Every request would
    #: otherwise be a write, and SQLite serialises writers.
    last_used_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    #: Retirement is a timestamp rather than a delete, so the audit question
    #: "what was this key doing before we killed it" stays answerable.
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    #: "author" or "admin" -- see core.auth.Role. Machines draft and try
    #: processors; putting one live takes an admin key or an admin person.
    role: Mapped[str] = mapped_column(String(10), default="author", server_default="author")


class User(Base):
    """
    A person who signs in to the panel.

    Separate from `api_keys` because the two are different kinds of secret:
    a key is 256 random bits a machine holds, a password is something a
    person chose and can be guessed. They are stored and checked differently
    (core/users.py), and only people get sessions.

    Still no owner column on the data: a user, like a key, sees everything.
    Roles decide what they may *change*, which is the risk the panel adds.
    """

    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    #: Lowercased on the way in, so "Ana@x.com" and "ana@x.com" are one person.
    email: Mapped[str] = mapped_column(String(254), unique=True, index=True)
    name: Mapped[str | None] = mapped_column(String(200))
    password_hash: Mapped[str] = mapped_column(String(200))
    role: Mapped[str] = mapped_column(String(10), default="author")
    #: Who this belongs to. See the tenancy note above Organisation.
    organisation_id: Mapped[str] = mapped_column(ForeignKey("organisations.id"), index=True)
    #: The Ledgerline team: sees every organisation's documents. A client's
    #: users never have this; they see their own organisation only.
    is_staff: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))

    #: Consecutive failures, and when signing in is allowed again after too
    #: many. Reset by a successful sign-in.
    failed_logins: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(UTCDateTime)

    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    last_login_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    #: Disabling rather than deleting, for the same reason keys are revoked:
    #: `created_by` on processor versions still has to mean someone.
    disabled_at: Mapped[datetime | None] = mapped_column(UTCDateTime)


class UserSession(Base):
    """
    One signed-in browser. Server-side, so signing out or disabling a user
    ends it immediately -- a signed cookie could only expire.

    The cookie holds a random token; only its SHA-256 is stored, as with API
    keys and for the same reason. The CSRF token is not a secret from the
    browser: it is the proof that a state-changing request came from a page
    the panel rendered, not from another site riding the cookie.
    """

    __tablename__ = "user_sessions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    csrf_token: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime)
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    user: Mapped[User] = relationship()


# --- Tenancy: rows that follow their document ---------------------------------

_FOLLOWS_DOCUMENT = (OcrResult, DocumentEvent, Job, Extraction, Transaction)


@event.listens_for(Session, "before_flush")
def _follow_the_document(session: Session, _context, _instances) -> None:
    """
    Give every row made from a document its document's organisation.

    In one place rather than at each of the dozen places such rows are
    created, so none of them can forget -- and a row whose document cannot be
    found keeps no organisation and fails its NOT NULL, loudly.
    """
    pending = {d.id: d for d in session.new if isinstance(d, Document)}
    for row in session.new:
        if isinstance(row, _FOLLOWS_DOCUMENT) and row.organisation_id is None:
            document = pending.get(row.document_id) or session.get(Document, row.document_id)
            if document is not None:
                row.organisation_id = document.organisation_id
