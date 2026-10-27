"""
The documents list, as a client sees it: GET /documents and its summary,
types and CSV export (DESIGN §9.4).

Everything here reads the list columns on `documents` -- processor, supplier,
the document's own date, its total -- which `summarise` sets when a document
is read. They are a read model: the ledger has rows only for documents that
passed their rules, and the list is for every document, the ones in review
most of all.

Tenancy is not handled here. Every query goes through a scoped session
(core/scoping.py), which limits it to the caller's organisation in the ORM
and again in Postgres row-level security.

Paging is by keyset, not offset. A cursor names the last row shown, and the
next page starts after it, so documents arriving while someone reads do not
shift the pages under them. The cursor is opaque to clients: base64 of the
sort and the last row's keys.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import csv
import io
import json
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Any, Literal
from zoneinfo import ZoneInfo

from pydantic import ValidationError
from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from core import money, repository
from core.models import Document, DocumentTypeDefinition
from doctypes.canonical import to_summary
from doctypes.definition import Definition
from doctypes.results import OUTCOMES, statuses_for
from doctypes.validate import coerce

Sort = Literal["received", "document_date"]

#: The order the summary counts outcomes in, and every one present even at zero.
OUTCOME_ORDER = ("processing", "done", "needs_review", "unsupported", "failed")


def summarise(document: Document, definition: Definition, data: dict) -> None:
    """Set what the list shows for `document`, from its coerced data as read (or corrected)."""
    for column, value in to_summary(definition, data).items():
        setattr(document, column, value)


@dataclass(frozen=True)
class Filters:
    outcome: str | None = None
    doc_type: str | None = None
    #: Matches the supplier or the file name, case-insensitively.
    q: str | None = None
    #: Received on or after / on or before these days, as days in `tz`: a
    #: client in Madrid asking for 26 October means their 26 October.
    received_from: date | None = None
    received_to: date | None = None
    tz: ZoneInfo = ZoneInfo("UTC")

    def clauses(self) -> list[Any]:
        where: list[Any] = []
        if self.outcome:
            where.append(Document.status.in_(statuses_for(self.outcome)))
        if self.doc_type:
            where.append(Document.doc_type == self.doc_type)
        if self.q and self.q.strip():
            needle = self.q.strip().lower()
            where.append(
                or_(
                    func.lower(Document.supplier).contains(needle, autoescape=True),
                    func.lower(Document.original_filename).contains(needle, autoescape=True),
                )
            )
        if self.received_from:
            where.append(Document.uploaded_at >= self._start_of(self.received_from))
        if self.received_to:
            where.append(
                Document.uploaded_at < self._start_of(self.received_to + timedelta(days=1))
            )
        return where

    def _start_of(self, day: date) -> datetime:
        return datetime.combine(day, time.min, tzinfo=self.tz).astimezone(UTC)


# --- Cursors ----------------------------------------------------------------------


class BadCursor(ValueError):
    """A cursor this list did not make, or made for another sort."""


def _encode(sort: Sort, document: Document) -> str:
    payload = {
        "s": sort,
        "d": document.document_date.isoformat() if document.document_date else None,
        "u": document.uploaded_at.isoformat(),
        "i": document.id,
    }
    return base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")


def _decode(sort: Sort, cursor: str) -> tuple[date | None, datetime, str]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded))
        if payload["s"] != sort:
            raise BadCursor("that cursor belongs to another sort order")
        day = date.fromisoformat(payload["d"]) if payload["d"] else None
        return day, datetime.fromisoformat(payload["u"]), str(payload["i"])
    except BadCursor:
        raise
    except (binascii.Error, ValueError, KeyError, TypeError) as exc:
        raise BadCursor("that cursor is not one this list made") from exc


def _after(sort: Sort, cursor: str) -> Any:
    """Rows after the cursor's row, in the list's order."""
    day, uploaded, ident = _decode(sort, cursor)
    later_in_tie = or_(
        Document.uploaded_at < uploaded,
        and_(Document.uploaded_at == uploaded, Document.id < ident),
    )
    if sort == "received":
        return later_in_tie
    # Newest document date first, undated last: they have no place in the order.
    if day is None:
        return and_(Document.document_date.is_(None), later_in_tie)
    return or_(
        Document.document_date < day,
        and_(Document.document_date == day, later_in_tie),
        Document.document_date.is_(None),
    )


def _ordered(sort: Sort) -> list[Any]:
    tail = [Document.uploaded_at.desc(), Document.id.desc()]
    if sort == "received":
        return tail
    return [Document.document_date.desc().nulls_last(), *tail]


# --- What the routes serve -----------------------------------------------------------


def page(
    session: Session,
    filters: Filters,
    *,
    sort: Sort = "received",
    cursor: str | None = None,
    limit: int = 25,
) -> tuple[list[Document], str | None]:
    """One page, newest first, and the cursor for the next (None on the last)."""
    stmt = select(Document).where(*filters.clauses()).order_by(*_ordered(sort))
    if cursor:
        stmt = stmt.where(_after(sort, cursor))
    rows = list(session.scalars(stmt.limit(limit + 1)))
    more = len(rows) > limit
    rows = rows[:limit]
    return rows, (_encode(sort, rows[-1]) if more and rows else None)


def summary(session: Session, filters: Filters) -> dict[str, Any]:
    """How many there are by outcome, and their totals per currency, never added across them."""
    by_outcome = dict.fromkeys(OUTCOME_ORDER, 0)
    counted = session.execute(
        select(Document.status, func.count()).where(*filters.clauses()).group_by(Document.status)
    )
    for status, count in counted:
        by_outcome[OUTCOMES.get(status, "processing")] += count
    totals = session.execute(
        select(Document.total_currency, func.sum(Document.total_amount))
        .where(
            *filters.clauses(),
            Document.total_amount.is_not(None),
            Document.total_currency.is_not(None),
        )
        .group_by(Document.total_currency)
        .order_by(Document.total_currency)
    )
    return {
        "documents": sum(by_outcome.values()),
        "by_outcome": by_outcome,
        "totals": [
            {"amount": money.to_str(Decimal(amount)), "currency": currency}
            for currency, amount in totals
        ],
    }


def labels(session: Session, names: Iterable[str]) -> dict[str, str]:
    """Each processor's name as people see it: its newest version's display name."""
    wanted = set(names)
    if not wanted:
        return {}
    newest = (
        select(
            DocumentTypeDefinition.name, func.max(DocumentTypeDefinition.version).label("version")
        )
        .where(DocumentTypeDefinition.name.in_(wanted))
        .group_by(DocumentTypeDefinition.name)
        .subquery()
    )
    rows = session.execute(
        select(DocumentTypeDefinition.name, DocumentTypeDefinition.definition).join(
            newest,
            and_(
                DocumentTypeDefinition.name == newest.c.name,
                DocumentTypeDefinition.version == newest.c.version,
            ),
        )
    )
    found = {name: (definition or {}).get("display_name") for name, definition in rows}
    return {name: found.get(name) or name.replace("_", " ").capitalize() for name in wanted}


def types(session: Session) -> list[dict[str, str]]:
    """The kinds of document this organisation's documents were read as, by label."""
    names = session.scalars(
        select(Document.doc_type).where(Document.doc_type.is_not(None)).distinct()
    )
    named = labels(session, [n for n in names if n])
    return sorted(
        ({"name": n, "label": label} for n, label in named.items()), key=lambda t: t["label"]
    )


def row(document: Document, label: dict[str, str]) -> dict[str, Any]:
    """One document as the list shows it (DocumentRow in portal/src/api/contract.ts)."""
    total = None
    if document.total_amount is not None:
        total = {"amount": money.to_str(document.total_amount), "currency": document.total_currency}
    return {
        "id": document.id,
        "filename": document.original_filename,
        "received_at": document.uploaded_at,
        "document_date": document.document_date,
        "supplier": document.supplier,
        "type": (
            {"name": document.doc_type, "label": label.get(document.doc_type, document.doc_type)}
            if document.doc_type
            else None
        ),
        "total": total,
        "outcome": OUTCOMES.get(document.status, "processing"),
    }


#: An export is one file; past this many rows, narrow the filters.
EXPORT_LIMIT = 50_000

#: What a spreadsheet would run as a formula. Supplier names and file names
#: come from documents and uploads -- from outside -- so a cell starting with
#: one of these is prefixed with an apostrophe, which spreadsheets show as text.
_FORMULA = ("=", "+", "-", "@", "\t", "\r")


def _cell(value: str | None) -> str:
    text = value or ""
    return f"'{text}" if text.startswith(_FORMULA) else text


def export(session: Session, filters: Filters, *, sort: Sort = "received") -> str:
    """The filtered list as CSV, every page of it."""
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(
        ["Received", "Document date", "Supplier", "Type", "Total", "Currency", "Status", "File"]
    )
    rows, _ = page(session, filters, sort=sort, limit=EXPORT_LIMIT)
    label = labels(session, {d.doc_type for d in rows if d.doc_type})
    for document in rows:
        listed = row(document, label)
        writer.writerow(
            [
                document.uploaded_at.isoformat(),
                document.document_date.isoformat() if document.document_date else "",
                _cell(document.supplier),
                _cell(listed["type"]["label"] if listed["type"] else None),
                listed["total"]["amount"] if listed["total"] else "",
                document.total_currency or "",
                listed["outcome"],
                _cell(document.original_filename),
            ]
        )
    return out.getvalue()


# --- Filling the columns for documents read before they existed ----------------------


def backfill(session: Session) -> int:
    """
    Set the list columns from each document's current extraction, where they
    are still empty. Idempotent: run it as often as you like.
    """
    filled = 0
    unfilled = session.scalars(select(Document).where(Document.doc_type.is_(None)))
    for document in unfilled:
        extraction = repository.current_extraction(session, document.id)
        if extraction is None:
            continue
        version = repository.get_document_type_version(
            session, extraction.doc_type, extraction.schema_version
        )
        values = (
            extraction.transformed_json
            if extraction.transformed_json is not None
            else extraction.raw_json
        )
        if version is None or values is None:
            continue
        try:
            definition = Definition.model_validate(version.definition)
        except ValidationError:
            continue
        data, _ = coerce(definition, values)
        summarise(document, definition, data)
        filled += 1
    session.commit()
    return filled


def main() -> None:
    from core.db import SessionLocal
    from core.scoping import Scope, set_scope

    parser = argparse.ArgumentParser(
        prog="python -m doctypes.listing", description=__doc__.split("\n\n")[0]
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("backfill", help="fill the list columns of documents read before they existed")
    parser.parse_args()
    with SessionLocal() as session:
        set_scope(session, Scope.everyone("doctypes.listing backfill"))
        print(f"filled the list columns of {backfill(session)} documents")


if __name__ == "__main__":
    main()
