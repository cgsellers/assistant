"""
Putting a validated document into the ledger: shared by the worker, when a
document is first read, and by corrections, when a person changes it.
"""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from core import money, repository
from core.models import Document, Extraction
from core.status import DocStatus
from doctypes.canonical import derived_columns, to_ledger
from doctypes.definition import Definition
from doctypes.transform import Processed

log = logging.getLogger("doctypes.ledger")


def commit(
    session: Session,
    document: Document,
    definition: Definition,
    extraction: Extraction,
    processed: Processed,
) -> None:
    """
    Write the canonical row, or record why the document stops at `validated`.

    Not every validated document reaches the ledger, and that is not a
    failure: a document type with no `canonical` mapping is one an analyst is
    still shaping. Stopping with the reason in the audit trail is more useful
    than inventing a row or failing the job. A row written here supersedes the
    document's earlier one, if it had one.
    """
    values, reason = to_ledger(definition, processed.data)
    if values is None:
        log.info("document=%s stays validated: %s", document.id[:8], reason)
        return
    values["derived_fields"] = derived_columns(definition, processed.data, processed.derived)

    transaction = repository.record_transaction(session, extraction=extraction, values=values)
    repository.record_transition(
        session,
        document,
        DocStatus.COMMITTED,
        {
            "transaction_id": transaction.id,
            "amount": money.to_str(transaction.amount),
            "currency": transaction.currency,
            "direction": transaction.direction,
            "derived": transaction.derived_fields,
        },
    )
