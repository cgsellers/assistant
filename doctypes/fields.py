"""
One document as the client portal shows it: GET /documents/{id}/fields
(DESIGN §9.4; DocumentFields in portal/src/api/contract.ts).

Shaped for a screen, where /result is shaped for an ERP and stays stable for
one: every field in its processor's order with a label people read, its type
(so money shows in the document's currency and dates are written out), the
problem that holds it in review, whether the transform worked it out, and
where on the original it was printed.
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from core import repository
from core.models import Correction, Document, Extraction
from core.status import DocStatus
from doctypes import listing
from doctypes.definition import Definition, FieldSpec, FieldType, RequiredRule
from doctypes.locate import locate
from doctypes.results import normalised_data

#: Short words a field name spells in lower case and people read in capitals.
ACRONYMS = {
    "id": "ID", "po": "PO", "vat": "VAT", "iva": "IVA", "iban": "IBAN", "bic": "BIC",
    "swift": "SWIFT", "nif": "NIF", "cif": "CIF", "nit": "NIT", "url": "URL", "sku": "SKU",
    "ean": "EAN", "eori": "EORI", "uk": "UK", "eu": "EU", "us": "US",
}  # fmt: skip


def label(name: str) -> str:
    """A field name as people read it: "vendor_tax_id" is "Vendor tax ID"."""
    words = [ACRONYMS.get(w, w) for w in name.split("_") if w]
    if not words:
        return name
    first = words[0] if words[0].isupper() else words[0].capitalize()
    return " ".join([first, *words[1:]])


def _required(definition: Definition, name: str, spec: FieldSpec) -> bool:
    return spec.required or any(
        isinstance(rule, RequiredRule) and rule.field == name for rule in definition.rules
    )


#: The validator's phrasing, said the way a client would: it talks about the
#: pipeline, the page talks about the document. Kept here rather than in the
#: validator, whose messages are also read by evals and the team's panel.
_PLAIN_WORDS = (
    ("is required but was not extracted", "is required, but wasn't found on the document"),
)


def _problems(definition: Definition, errors: list[str]) -> tuple[dict[str, str], list[str]]:
    """
    Each validation message, at the field it is about, or with the document's
    own when it is about none. A message names its field first -- "total is
    required but was not extracted", "line_items[2].amount is null" -- and the
    names in it become labels, so it reads as a sentence about the page.
    """
    names = sorted(definition.fields, key=len, reverse=True)
    pattern = re.compile(r"\b(" + "|".join(map(re.escape, names)) + r")\b") if names else None

    def named(match: re.Match) -> str:
        shown = label(match.group(1))
        # A label starts a sentence with a capital, and is lower case inside
        # one -- unless it begins with an acronym ("PO number", "IVA").
        first = shown.split(" ")[0]
        if match.start() == 0 or first.isupper():
            return shown
        return shown[:1].lower() + shown[1:]

    def readable(message: str) -> str:
        text = pattern.sub(named, message) if pattern else message
        for said, meant in _PLAIN_WORDS:
            text = text.replace(said, meant)
        return text[:1].upper() + text[1:]

    by_field: dict[str, str] = {}
    general: list[str] = []
    for message in errors:
        lead = re.match(r"[a-z][a-z0-9_]*", message)
        name = lead.group() if lead else None
        if name in definition.fields:
            by_field[name] = (
                f"{by_field[name]} {readable(message)}" if name in by_field else readable(message)
            )
        else:
            general.append(readable(message))
    return by_field, general


def _value(spec: FieldSpec, value: Any) -> Any:
    """Wire values as the contract has them; a list's rows as plain dicts."""
    if spec.type is FieldType.LIST:
        return [row for row in value if isinstance(row, dict)] if isinstance(value, list) else []
    return value


#: What a client is told happened, by status reached. Steps a client has no
#: use for -- text read, model answered -- are left out.
_HISTORY = {
    DocStatus.UPLOADED: "Received",
    DocStatus.NEEDS_REVIEW: "Held for review",
    DocStatus.VALIDATED: "Ready",
    DocStatus.COMMITTED: "Added to the ledger",
    DocStatus.UNSUPPORTED: "Not recognised",
    DocStatus.OCR_FAILED: "Couldn't be read",
    DocStatus.REJECTED: "Couldn't be read",
    DocStatus.REPROCESSING: "Being read again",
}


def _history(document: Document, labels: dict[str, str]) -> list[dict[str, Any]]:
    entries = []
    for event in document.events:
        if event.to_status == DocStatus.CLASSIFIED:
            doc_type = (event.detail or {}).get("doc_type")
            what = f"Recognised as {labels.get(doc_type, doc_type).lower()}" if doc_type else None
        else:
            what = _HISTORY.get(event.to_status)
        if what:
            entries.append({"at": event.created_at, "what": what, "by": None})
    return entries


def _corrections(session: Session, document: Document) -> list[Correction]:
    return list(
        session.scalars(
            select(Correction)
            .where(Correction.document_id == document.id)
            .order_by(Correction.revision)
        )
    )


def _corrected(corrections: list[Correction]) -> dict[str, dict[str, Any]]:
    """
    Who last changed each field, and what was read before anyone did: the
    first correction's "was", since later ones only record the one before.
    """
    corrected: dict[str, dict[str, Any]] = {}
    for correction in corrections:
        for name, change in (correction.changes or {}).items():
            was = corrected[name]["was"] if name in corrected else change.get("was")
            corrected[name] = {"by": correction.by, "at": correction.created_at, "was": was}
    return corrected


def _correction_history(
    corrections: list[Correction], definition: Definition | None
) -> list[dict[str, Any]]:
    entries = []
    for correction in corrections:
        names = list(correction.changes or {})
        if names:
            labels = ", ".join(
                label(n) if definition and n in definition.fields else n for n in names
            )
            entries.append(
                {"at": correction.created_at, "what": f"Corrected {labels}", "by": correction.by}
            )
        if correction.approved:
            entries.append(
                {"at": correction.created_at, "what": "Approved as it is", "by": correction.by}
            )
    return entries


def _read_at(session: Session, document: Document):
    """When the model last read it: corrections are extractions too, but nobody read anything."""
    return session.scalar(
        select(Extraction.created_at)
        .where(
            Extraction.document_id == document.id,
            Extraction.is_trial.is_(False),
            Extraction.model_used != "correction",
        )
        .order_by(Extraction.created_at.desc())
        .limit(1)
    )


def _definition(session: Session, extraction: Extraction) -> Definition | None:
    """The processor version that read it -- not the live one, which may differ by now."""
    row = repository.get_document_type_version(
        session, extraction.doc_type, extraction.schema_version
    )
    if row is None:
        return None
    try:
        return Definition.model_validate(row.definition)
    except ValidationError:
        return None


def document_fields(session: Session, document: Document) -> dict[str, Any]:
    """Everything the document page shows about one document."""
    extraction = repository.current_extraction(session, document.id)
    definition = _definition(session, extraction) if extraction else None
    names = {n for n in (document.doc_type, extraction.doc_type if extraction else None) if n}
    labels = listing.labels(session, names)
    unread = {
        "document": {
            **listing.row(document, labels),
            "mime_type": document.mime_type,
            "size_bytes": document.size_bytes,
        },
        "processor": None,
        "currency": None,
        "fields": [],
        "problems": [],
        "read_at": None,
        "revision": document.revision,
        "history": _history(document, labels),
    }
    if extraction is None or definition is None:
        return unread

    data = normalised_data(session, extraction) or {}
    corrections = _corrections(session, document)
    corrected = _corrected(corrections)
    derived = set(extraction.derived_fields or [])
    by_field, general = _problems(definition, list(extraction.validation_errors or []))
    ocr = repository.get_ocr_result(session, document.id)
    where = locate(
        definition, data, ocr.layout_blocks if ocr else None, ocr.page_sizes if ocr else None
    )

    fields = []
    for name, spec in definition.fields.items():
        common = {
            "name": name,
            "label": label(name),
            "required": _required(definition, name, spec),
            "derived": name in derived,
            "problem": by_field.get(name),
            # A list's rows are replaced whole, so there is no one value it was.
            "corrected": (
                {**corrected[name], "was": None}
                if name in corrected and spec.type is FieldType.LIST
                else corrected.get(name)
            ),
        }
        value = _value(spec, data.get(name))
        if spec.type is FieldType.LIST:
            columns = spec.item_fields or {}
            fields.append(
                {
                    **common,
                    "type": "list",
                    "columns": [
                        {"name": c, "label": label(c), "type": str(s.type)}
                        for c, s in columns.items()
                    ],
                    "rows": [{c: row.get(c) for c in columns} for row in value],
                    "row_locations": [where.get(f"{name}.{i}") for i in range(len(value))],
                }
            )
        else:
            fields.append(
                {
                    **common,
                    "type": str(spec.type),
                    "value": value,
                    # Nothing to point at for a value that was worked out, not printed.
                    "location": None if name in derived else where.get(name),
                }
            )

    currency_field = definition.canonical.currency if definition.canonical else None
    currency = data.get(currency_field) if currency_field else None
    return {
        **unread,
        "processor": {
            "name": definition.name,
            "label": labels.get(definition.name, definition.name),
            "version": extraction.schema_version,
        },
        "currency": currency if isinstance(currency, str) else document.total_currency,
        "fields": fields,
        "problems": general,
        "read_at": _read_at(session, document) or extraction.created_at,
        "history": sorted(
            unread["history"] + _correction_history(corrections, definition),
            key=lambda entry: entry["at"],
        ),
    }
