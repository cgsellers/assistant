"""
Request and response shapes for the API.

These are deliberately separate from the SQLAlchemy models: the database schema
is an internal detail that changes for storage reasons, while these are a public
contract. `from_attributes` lets Pydantic build them straight from ORM objects.
"""

from __future__ import annotations

from datetime import datetime

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
