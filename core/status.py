"""
Status vocabularies for documents and queue jobs.

`StrEnum` members compare equal to their string value, so these can be written
straight into a `String` column and compared against raw DB values without
converting: `doc.status == DocStatus.UPLOADED` works either way.
"""

from enum import StrEnum


class DocStatus(StrEnum):
    """
    One document has exactly one status. These mirror the state machine in the
    README; every transition is recorded as a row in `document_events`.
    """

    # Phase 1 — ingestion
    UPLOADED = "uploaded"
    DUPLICATE = "duplicate"
    OCR_DONE = "ocr_done"
    OCR_FAILED = "ocr_failed"

    # Phase 2 — routing and extraction
    CLASSIFIED = "classified"
    UNSUPPORTED = "unsupported"
    EXTRACTED = "extracted"
    VALIDATED = "validated"

    # Phase 4 — human review
    NEEDS_REVIEW = "needs_review"
    REJECTED = "rejected"

    # Phase 2+ — committed to the ledger, then delivered onward
    COMMITTED = "committed"
    SYNCED = "synced"

    # Phase 5 — re-run from stored OCR text after a schema bump
    REPROCESSING = "reprocessing"


class JobStatus(StrEnum):
    """Lifecycle of a row in the `jobs` queue table."""

    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
