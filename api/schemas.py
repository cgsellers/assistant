"""
Request and response shapes for the API.

These are deliberately separate from the SQLAlchemy models: the database schema
is an internal detail that changes for storage reasons, while these are a public
contract. `from_attributes` lets Pydantic build them straight from ORM objects.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class Base64Upload(BaseModel):
    """JSON alternative to multipart, for callers that already hold bytes."""

    content_b64: str = Field(description="Base64-encoded file contents")
    filename: str | None = Field(default=None, description="Original filename, if known")


class DocumentEventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    from_status: str | None
    to_status: str
    detail: dict | None
    created_at: datetime


class DocumentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    sha256: str
    mime_type: str
    original_filename: str | None
    size_bytes: int
    status: str
    uploaded_at: datetime

    # Not a column: set by the route so a client can tell "I created this" from
    # "this already existed". Upload is idempotent, so both return 200.
    duplicate: bool = False


class OcrResultOut(BaseModel):
    """
    Stored OCR output for a document.

    `layout_blocks` is omitted by default -- a dense invoice produces hundreds
    of entries and drowns the response when all you wanted was the text.
    """

    model_config = ConfigDict(from_attributes=True)

    engine: str
    engine_version: str | None
    params: dict | None
    full_text: str
    mean_confidence: float | None
    created_at: datetime

    block_count: int = 0
    layout_blocks: list | None = None


class DocumentDetail(DocumentOut):
    """A document plus its audit trail."""

    events: list[DocumentEventOut] = []


class ProcessorRef(BaseModel):
    name: str
    version: int
    profile: str | None


class LedgerRowOut(BaseModel):
    id: str
    direction: str
    amount: str
    tax_amount: str | None
    currency: str | None
    occurred_on: str | None
    counterparty: str | None
    counterparty_tax_id: str | None
    #: Ledger columns a transform worked out rather than read off the page.
    derived: list[str]


class ResultOut(BaseModel):
    """
    A document's result, for a client system to import. See doctypes/results.py.

    Poll until `outcome` is not "processing". "done" means every rule passed;
    "needs_review" still carries `data`, with `problems` saying what failed.
    """

    document_id: str
    #: The internal status, for support. Branch on `outcome` instead.
    status: str
    outcome: Literal["processing", "done", "needs_review", "unsupported", "failed"]
    processor: ProcessorRef | None
    #: Every field the processor declares, normalised: money as "7.15", dates
    #: as YYYY-MM-DD, null where the document did not say. After the transform.
    data: dict | None
    #: What the model read, before the transform -- the evidence.
    read: dict | None
    #: Fields in `data` the transform worked out rather than read.
    derived: list[str]
    problems: list[str]
    transaction: LedgerRowOut | None
    extracted_at: str | None


# --- The documents list (DESIGN §9.4; portal/src/api/contract.ts) -------------------

Outcome = Literal["processing", "done", "needs_review", "unsupported", "failed"]


class MoneyOut(BaseModel):
    #: Exact decimal as a string, e.g. "104.88" -- never a float.
    amount: str
    #: ISO 4217, or null when the document did not say.
    currency: str | None


class DocTypeOut(BaseModel):
    name: str
    label: str


class DocumentRowOut(BaseModel):
    """One row of GET /documents."""

    id: str
    filename: str | None
    #: When it reached Ledgerline.
    received_at: datetime
    #: The document's own date, as printed.
    document_date: date | None
    #: Who issued it: the supplier on an invoice, the shop on a receipt.
    supplier: str | None
    type: DocTypeOut | None
    total: MoneyOut | None
    outcome: Outcome


class DocumentListOut(BaseModel):
    items: list[DocumentRowOut]
    #: Pass back as `cursor` for the next page; null on the last one.
    next_cursor: str | None


class ByOutcomeOut(BaseModel):
    processing: int
    done: int
    needs_review: int
    unsupported: int
    failed: int


class DocumentSummaryOut(BaseModel):
    documents: int
    by_outcome: ByOutcomeOut
    #: Per currency, never added across currencies.
    totals: list[MoneyOut]


# --- One document, for the portal (DocumentFields in portal/src/api/contract.ts) -----

FieldKind = Literal["string", "integer", "number", "money", "date", "boolean"]
#: Money as "104.88" and dates as YYYY-MM-DD: the same wire form as /result.
FieldValue = str | int | float | bool | None


class LocationOut(BaseModel):
    """Where on the original a value was printed: a page, and a box on it as fractions."""

    page: int
    #: [left, top, right, bottom], each from 0 to 1, from the page's top-left corner.
    box: tuple[float, float, float, float]


class CorrectionOut(BaseModel):
    by: str
    at: datetime
    was: FieldValue


class _FieldOut(BaseModel):
    name: str
    label: str
    required: bool
    #: Worked out by the processor's transform rather than read off the page.
    derived: bool
    #: Why this field holds the document in review.
    problem: str | None
    corrected: CorrectionOut | None


class ScalarFieldOut(_FieldOut):
    type: FieldKind
    value: FieldValue
    location: LocationOut | None


class ColumnOut(BaseModel):
    name: str
    label: str
    type: FieldKind


class ListFieldOut(_FieldOut):
    type: Literal["list"]
    columns: list[ColumnOut]
    rows: list[dict[str, FieldValue]]
    row_locations: list[LocationOut | None]


class FieldsDocumentOut(DocumentRowOut):
    mime_type: str
    size_bytes: int


class ProcessorRefOut(BaseModel):
    name: str
    label: str
    version: int


class HistoryEntryOut(BaseModel):
    at: datetime
    what: str
    by: str | None


class DocumentFieldsOut(BaseModel):
    """GET /documents/{id}/fields: one document, shaped for the portal's page."""

    document: FieldsDocumentOut
    processor: ProcessorRefOut | None
    #: The document's currency, for every money field on it.
    currency: str | None
    fields: list[ScalarFieldOut | ListFieldOut]
    #: Problems not about any one field.
    problems: list[str]
    read_at: datetime | None
    #: Goes up with every change; a correction names the one it was made against.
    revision: int
    history: list[HistoryEntryOut]
