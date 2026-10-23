"""
The rules the ledger rests on, held by the database itself.

Code demotes the old row before promoting the new one, and every path is
tested to do so. These tests go around that code -- writing rows directly, as
a racing second worker effectively would -- and check the database refuses.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy.exc import IntegrityError

from core import repository
from core.models import DocumentTypeDefinition, Extraction, Transaction

from .conftest import TINY_PNG, receipt_definition


@pytest.fixture
def extraction(session):
    document, _ = repository.ingest(
        session, data=TINY_PNG, mime_type="image/png", original_filename="a.png"
    )
    return repository.record_extraction(
        session, document_id=document.id, doc_type="receipt", schema_version=1,
        model_used="fake", raw_json={"total": "1.00"},
    )


def _refused(session, row) -> bool:
    session.add(row)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        return True
    return False


def test_a_document_cannot_have_two_current_extractions(session, extraction):
    second = Extraction(
        document_id=extraction.document_id, doc_type="receipt", schema_version=1,
        model_used="fake", is_current=True,
    )
    assert _refused(session, second)


def test_superseded_and_trial_extractions_are_unlimited(session, extraction):
    for current, trial in ((False, False), (False, True), (False, True)):
        row = Extraction(
            document_id=extraction.document_id, doc_type="receipt", schema_version=1,
            model_used="fake", is_current=current, is_trial=trial,
        )
        assert not _refused(session, row)


def test_a_document_cannot_have_two_current_ledger_rows(session, extraction):
    """The one that would count a receipt twice."""
    repository.record_transaction(
        session, extraction=extraction,
        values={"source_type": "receipt", "direction": "out", "amount": Decimal("1.00")},
    )
    racing = Transaction(
        source_extraction_id=extraction.id, document_id=extraction.document_id,
        source_type="receipt", direction="out", amount=Decimal("1.00"), is_current=True,
    )
    assert _refused(session, racing)


def test_a_processor_cannot_have_two_active_versions(session):
    repository.create_document_type_version(session, receipt_definition())
    racing = DocumentTypeDefinition(
        name="receipt", version=2, definition=receipt_definition().model_dump(mode="json"),
        is_active=True, is_published=True,
    )
    assert _refused(session, racing)


def test_the_normal_paths_still_supersede_cleanly(session, extraction):
    """Demote-then-insert, inside one transaction, satisfies the constraint."""
    for amount in ("1.00", "2.00", "3.00"):
        repository.record_transaction(
            session, extraction=extraction,
            values={"source_type": "receipt", "direction": "out", "amount": Decimal(amount)},
        )
    current = session.query(Transaction).filter_by(is_current=True).one()
    assert current.amount == Decimal("3.00")
