"""
A document's result, in the shape a client system receives it.

The point of the pipeline, from the outside: an ERP sends a PDF and gets back
JSON it can import. That client should not need to know Ledgerline's internal
state machine, how a transform works, or which spelling of a decimal a
Spanish receipt used. So the result says:

  * **outcome** -- one of five words a client can branch on, mapped from the
    document's status.
  * **data** -- every field the processor declares, normalised: money as
    "7.15" whatever the page printed, dates as YYYY-MM-DD, null for what the
    document did not carry. After the transform, so a derived total is here.
  * **read** and **derived** -- what the model actually read, and which
    fields the transform worked out, so a client can tell a printed value
    from a computed one.
  * **problems** -- why a document is waiting for review. The data is still
    returned: the client decides whether a flagged total is usable.
  * **processor** and **transaction** -- which version produced it, and the
    ledger row it became.
"""

from __future__ import annotations

import logging
from typing import Any

from pydantic import ValidationError
from sqlalchemy.orm import Session

from core import money, repository
from core.models import Document, Transaction
from core.status import DocStatus
from doctypes.definition import Definition
from doctypes.transform import to_wire
from doctypes.validate import coerce

log = logging.getLogger("doctypes.results")

#: Document status -> what a client needs to know. Every status maps, so a
#: new one added to DocStatus without a decision here fails a test.
OUTCOMES: dict[str, str] = {
    DocStatus.UPLOADED: "processing",
    DocStatus.OCR_DONE: "processing",
    DocStatus.CLASSIFIED: "processing",
    DocStatus.EXTRACTED: "processing",
    DocStatus.REPROCESSING: "processing",
    DocStatus.VALIDATED: "done",
    DocStatus.COMMITTED: "done",
    DocStatus.SYNCED: "done",
    DocStatus.NEEDS_REVIEW: "needs_review",
    DocStatus.UNSUPPORTED: "unsupported",
    DocStatus.OCR_FAILED: "failed",
    DocStatus.REJECTED: "failed",
}


def document_result(session: Session, document: Document) -> dict[str, Any]:
    """Everything a client gets for one document, whatever state it is in."""
    extraction = repository.current_extraction(session, document.id)
    transaction = repository.current_transaction(session, document.id)

    result: dict[str, Any] = {
        "document_id": document.id,
        "status": document.status,
        "outcome": OUTCOMES.get(document.status, "processing"),
        "processor": None,
        "data": None,
        "read": None,
        "derived": [],
        "problems": [],
        "transaction": _transaction(transaction) if transaction else None,
        "extracted_at": None,
    }
    if extraction is None:
        return result

    result.update(
        processor={
            "name": extraction.doc_type,
            "version": extraction.schema_version,
            "profile": extraction.profile,
        },
        data=_normalised(session, extraction),
        read=extraction.raw_json,
        derived=extraction.derived_fields or [],
        problems=extraction.validation_errors or [],
        extracted_at=extraction.created_at.isoformat(),
    )
    return result


def _normalised(session: Session, extraction) -> dict | None:
    """
    The values the ledger was built from, in exact wire form.

    Coerced against the version that produced them -- not the live one, which
    may have different fields by now -- so every declared field is present and
    every value has its declared type.
    """
    values = (
        extraction.transformed_json
        if extraction.transformed_json is not None
        else extraction.raw_json
    )
    if values is None:
        return None
    row = repository.get_document_type_version(
        session, extraction.doc_type, extraction.schema_version
    )
    try:
        definition = Definition.model_validate(row.definition) if row else None
    except ValidationError:
        definition = None
    if definition is None:
        # The version no longer validates, or is gone. The values as stored
        # beat no values; the client still has `read` beside them.
        log.warning(
            "result for %s: %s v%s unavailable, returning values uncoerced",
            extraction.document_id, extraction.doc_type, extraction.schema_version,
        )
        return values
    coerced, _ = coerce(definition, values)
    return to_wire(coerced)


def _transaction(row: Transaction) -> dict[str, Any]:
    return {
        "id": row.id,
        "direction": row.direction,
        "amount": money.to_str(row.amount),
        "tax_amount": money.to_str(row.tax_amount) if row.tax_amount is not None else None,
        "currency": row.currency,
        "occurred_on": row.occurred_on.isoformat() if row.occurred_on else None,
        "counterparty": row.counterparty,
        "counterparty_tax_id": row.counterparty_tax_id,
        "derived": row.derived_fields or [],
    }
