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
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import ValidationError
from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session

from core import money, repository
from core.models import Document, Extraction, Job, Transaction
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


# --- Browsing: the admin Documents pages ---------------------------------------
# No API endpoint lists documents yet, on purpose: before tenancy (DESIGN §9.3)
# it would hand every key every client's documents. The panel is for the
# Ledgerline team, which may see them all.


@dataclass(frozen=True)
class DocumentRow:
    id: str
    filename: str | None
    uploaded_at: datetime
    status: str
    outcome: str
    processor: str | None
    version: int | None
    amount: str | None
    currency: str | None
    #: Everything billed for this document, failed attempts included; trials not.
    cost_usd: Decimal


def statuses_for(outcome: str) -> list[str]:
    """The internal statuses a client-facing outcome stands for."""
    return [status for status, out in OUTCOMES.items() if out == outcome]


def list_documents(
    session: Session,
    *,
    outcome: str | None = None,
    processor: str | None = None,
    since: datetime | None = None,
    before: datetime | None = None,
    limit: int = 50,
) -> list[DocumentRow]:
    """
    Newest first, one query. `before` is the cursor for the next page: the
    upload time of the last row shown, which stays correct while new documents
    keep arriving, where an offset would shift under the reader.
    """
    cost = (
        select(func.coalesce(func.sum(Extraction.cost_usd), 0))
        .where(Extraction.document_id == Document.id, Extraction.is_trial.is_(False))
        .correlate(Document)
        .scalar_subquery()
    )
    stmt = (
        select(
            Document,
            Extraction.doc_type,
            Extraction.schema_version,
            Transaction.amount,
            Transaction.currency,
            cost,
        )
        .outerjoin(
            Extraction,
            and_(Extraction.document_id == Document.id, Extraction.is_current.is_(True)),
        )
        .outerjoin(
            Transaction,
            and_(Transaction.document_id == Document.id, Transaction.is_current.is_(True)),
        )
        .order_by(Document.uploaded_at.desc(), Document.id.desc())
        .limit(max(1, min(limit, 200)))
    )
    if outcome:
        stmt = stmt.where(Document.status.in_(statuses_for(outcome)))
    if processor:
        stmt = stmt.where(Extraction.doc_type == processor)
    if since is not None:
        stmt = stmt.where(Document.uploaded_at >= since)
    if before is not None:
        stmt = stmt.where(Document.uploaded_at < before)

    return [
        DocumentRow(
            id=document.id,
            filename=document.original_filename,
            uploaded_at=document.uploaded_at,
            status=document.status,
            outcome=OUTCOMES.get(document.status, "processing"),
            processor=doc_type,
            version=version,
            amount=money.to_str(amount) if amount is not None else None,
            currency=currency,
            cost_usd=Decimal(str(spent or 0)),
        )
        for document, doc_type, version, amount, currency, spent in session.execute(stmt)
    ]


def document_detail(session: Session, document: Document) -> dict[str, Any]:
    """Everything known about one document, for the page that explains it."""
    events = list(document.events)
    timeline = [
        {
            "status": event.to_status,
            "at": event.created_at,
            # How long it took to get here from the step before: where the
            # time went, without timing anything twice.
            "took": (event.created_at - events[i - 1].created_at) if i else None,
            "detail": event.detail or {},
        }
        for i, event in enumerate(events)
    ]
    attempts = list(
        session.scalars(
            select(Extraction)
            .where(Extraction.document_id == document.id)
            .order_by(Extraction.created_at)
        )
    )
    jobs = list(
        session.scalars(
            select(Job).where(Job.document_id == document.id).order_by(Job.created_at)
        )
    )
    billed = [a.cost_usd for a in attempts if not a.is_trial and a.cost_usd is not None]
    trials = [a.cost_usd for a in attempts if a.is_trial and a.cost_usd is not None]
    return {
        "document": document,
        "result": document_result(session, document),
        "timeline": timeline,
        "attempts": attempts,
        "jobs": jobs,
        "ocr": repository.get_ocr_result(session, document.id),
        "cost_usd": sum(billed, Decimal("0")),
        "trial_cost_usd": sum(trials, Decimal("0")),
        "total_elapsed": (
            events[-1].created_at - events[0].created_at if len(events) > 1 else None
        ),
    }
