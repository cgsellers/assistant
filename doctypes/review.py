"""
A person correcting what was read from a document, or approving it as it is
-- PATCH /documents/{id}/fields (DESIGN §9.4).

A correction is not an edit of stored values. The values a person sends are
laid over what the model read, and the document goes through its processor
again -- coercion, transform, rules -- exactly as the worker put it through
the first time. That makes it a new extraction, current from then on, so
everything that reads the current extraction follows: the list, the page,
/result for an ERP, the ledger. A corrected subtotal recomputes a total the
transform worked out; a total typed in stays as typed.

Then the document moves on as the rules say: ready and into the ledger, or
still in review -- unless the person approves it with its problems, which is
recorded as such. A committed document corrected into a state that fails its
rules has its ledger row withdrawn: the ledger must not keep a figure its own
client has disputed.

Who changed what, from what, is a `corrections` row per save. Saves are
serialised by the document's revision: each names the revision it was made
against, and one that lost a race is refused rather than silently applied
over values its author never saw.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from core import repository
from core.models import Correction, Document
from core.status import DocStatus
from doctypes import ledger, listing
from doctypes.definition import Definition, FieldSpec, FieldType
from doctypes.results import normalised_data
from doctypes.transform import process


class NothingRead(Exception):
    """The document has no extraction to correct yet."""


class Stale(Exception):
    """Someone saved since the caller loaded the document."""


class Refused(Exception):
    """Values that do not fit their fields, by field."""

    def __init__(self, fields: dict[str, str]):
        super().__init__("Some values don't fit their fields.")
        self.fields = fields


_MONEY = re.compile(r"^-?\d{1,10}(\.\d{1,4})?$")
#: As long as a field's description may be: generous for text that was printed.
_TEXT_LIMIT = 2000


def _fits(spec: FieldSpec, value: Any) -> str | None:
    """Why `value` does not fit `spec`, or None. Null always fits: a field may be cleared."""
    if value is None:
        return None
    kind = spec.type
    if kind is FieldType.MONEY:
        ok = isinstance(value, str) and bool(_MONEY.match(value))
        return None if ok else 'An amount, like "1234.56"'
    if kind is FieldType.DATE:
        if not (isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value)):
            return "A date, YYYY-MM-DD"
        try:
            date.fromisoformat(value)
        except ValueError:
            return "A date, YYYY-MM-DD"
        return None
    if kind is FieldType.INTEGER:
        return None if isinstance(value, int) and not isinstance(value, bool) else "A whole number"
    if kind is FieldType.NUMBER:
        number = isinstance(value, int | float) and not isinstance(value, bool)
        return None if number else "A number"
    if kind is FieldType.BOOLEAN:
        return None if isinstance(value, bool) else "Yes or no"
    if kind is FieldType.STRING:
        if not isinstance(value, str):
            return "Text"
        return None if len(value) <= _TEXT_LIMIT else f"At most {_TEXT_LIMIT} characters"
    if kind is FieldType.LIST:
        if not isinstance(value, list) or not all(isinstance(row, dict) for row in value):
            return "A list of rows"
        columns = spec.item_fields or {}
        for index, row in enumerate(value):
            for column, cell in row.items():
                if column not in columns:
                    return f"Row {index + 1}: no column called {column!r}"
                if (wrong := _fits(columns[column], cell)) is not None:
                    return f"Row {index + 1}, {column}: {wrong}"
        return None
    return "Not a value this field can hold"


def refusals(definition: Definition, values: dict[str, Any]) -> dict[str, str]:
    """Each value that does not fit its field, by field name; empty when all do."""
    wrong = {}
    for name, value in values.items():
        spec = definition.fields.get(name)
        if spec is None:
            wrong[name] = "No such field"
        elif (problem := _fits(spec, value)) is not None:
            wrong[name] = problem
    return wrong


def _definition(session: Session, doc_type: str, version: int) -> Definition | None:
    row = repository.get_document_type_version(session, doc_type, version)
    if row is None:
        return None
    try:
        return Definition.model_validate(row.definition)
    except ValidationError:
        return None


def correct(
    session: Session,
    document: Document,
    *,
    revision: int,
    values: dict[str, Any],
    approve: bool,
    by: str,
) -> None:
    """
    Apply a person's corrections and/or approval to `document`, and commit.
    Raises NothingRead, Stale or Refused, having changed nothing.
    """
    # Serialise saves on the document: the revision check and the increment
    # must not interleave with another save's. A no-op on SQLite, which
    # serialises writers anyway.
    session.execute(select(Document.id).where(Document.id == document.id).with_for_update())
    session.refresh(document)

    extraction = repository.current_extraction(session, document.id)
    definition = (
        _definition(session, extraction.doc_type, extraction.schema_version) if extraction else None
    )
    if extraction is None or definition is None:
        raise NothingRead
    if revision != document.revision:
        raise Stale
    if wrong := refusals(definition, values):
        raise Refused(wrong)

    before = normalised_data(session, extraction) or {}
    read = dict(extraction.raw_json or {})
    if values:
        # Laid over what the model read, not over what the transform made of
        # it: a total the transform worked out is worked out again.
        read.update(values)
        processed = process(definition, read)
        extraction = repository.record_extraction(
            session,
            document_id=document.id,
            doc_type=extraction.doc_type,
            schema_version=extraction.schema_version,
            model_used="correction",
            profile=extraction.profile,
            raw_json=read,
            validation_errors=processed.errors,
            transformed_json=processed.transformed,
            derived_fields=processed.derived if definition.transform else None,
        )
    else:
        processed = process(definition, read)

    document.revision += 1
    session.add(
        Correction(
            document_id=document.id,
            extraction_id=extraction.id,
            revision=document.revision,
            by=by,
            changes={
                name: {"was": before.get(name), "now": value} for name, value in values.items()
            },
            approved=approve,
        )
    )
    listing.summarise(document, definition, processed.data)

    held = bool(processed.errors) and not approve
    if held:
        if repository.withdraw_transaction(session, document.id):
            repository.record_transition(
                session, document, DocStatus.NEEDS_REVIEW, {"withdrawn_from_ledger": True, "by": by}
            )
        elif document.status != DocStatus.NEEDS_REVIEW:
            repository.record_transition(session, document, DocStatus.NEEDS_REVIEW, {"by": by})
        session.commit()
        return

    repository.record_transition(
        session, document, DocStatus.VALIDATED, {"by": by, "approved": approve}
    )
    session.commit()
    ledger.commit(session, document, definition, extraction, processed)
    session.commit()
