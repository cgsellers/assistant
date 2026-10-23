"""
The analyst's tools: deterministic queries over the canonical ledger.

The principle this phase exists to demonstrate is that **the analyst never
does arithmetic**. A model asked "how much did I spend at Mercadona" that
answers from numbers in its context will sometimes be wrong, and will be
wrong in a way nobody can see. So the model's job is to choose a tool and
explain the answer; the sums happen here, in code with tests.

Everything is read-only. There is no tool that writes, so an approval gate is
not a rule the agent is asked to follow -- it is the absence of a capability.
The agent can *propose* a change; applying one is a separate human action
through the API.

Two things fall out of taking the ledger seriously:

  * **Amounts never cross currencies.** A total is reported per currency, so
    8.30 EUR and 8.30 USD can never silently become 16.60 of nothing. Rows
    whose currency the document did not state get their own bucket rather
    than being folded into the majority.
  * **Every figure carries its sources.** Each result lists the document ids
    behind it, so an answer can cite the receipts it came from and a reader
    can go and look at them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from core.models import Document, Extraction, Transaction
from core.money import to_str as amount_str

#: No question needs every row, and an unbounded result is how a context
#: window gets filled with data the model then has to do arithmetic over.
DEFAULT_LIMIT = 20
MAX_LIMIT = 200


@dataclass(frozen=True)
class CurrencyTotal:
    """
    A total in one currency. Never summed across currencies.

    Not to be confused with core.models.Money, the column type -- this was
    once also called Money, and two unrelated classes sharing a name in one
    small codebase is a trap for every reader.
    """

    currency: str | None
    total: Decimal
    count: int

    def as_dict(self) -> dict:
        return {
            "currency": self.currency or "unstated",
            "total": amount_str(self.total),
            "transactions": self.count,
        }


@dataclass(frozen=True)
class Totals:
    """What a spend query came to, split by currency."""

    amounts: list[CurrencyTotal] = field(default_factory=list)
    tax: list[CurrencyTotal] = field(default_factory=list)
    document_ids: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "totals": [m.as_dict() for m in self.amounts],
            "tax": [m.as_dict() for m in self.tax],
            "sources": self.document_ids,
            "note": (
                "Totals are split by currency and must not be added together."
                if len(self.amounts) > 1
                else None
            ),
        }


def _live(direction: str | None = "out"):
    """
    The base query: current rows only.

    `is_current` is the whole reason a re-extracted receipt does not get
    counted twice, so every query starts here rather than remembering to add
    it.
    """
    stmt = select(Transaction).where(Transaction.is_current.is_(True))
    if direction is not None:
        stmt = stmt.where(Transaction.direction == direction)
    return stmt


def _window(stmt, start: date | None, end: date | None):
    if start is not None:
        stmt = stmt.where(Transaction.occurred_on >= start)
    if end is not None:
        stmt = stmt.where(Transaction.occurred_on <= end)
    return stmt


def _by_currency(rows: list[Transaction], attribute: str) -> list[CurrencyTotal]:
    buckets: dict[str | None, list[Decimal]] = {}
    for row in rows:
        value = getattr(row, attribute)
        if value is None:
            continue
        buckets.setdefault(row.currency, []).append(value)
    return [
        CurrencyTotal(currency=currency, total=sum(values, Decimal("0")), count=len(values))
        for currency, values in sorted(buckets.items(), key=lambda kv: (kv[0] or "",))
    ]


def total_spend(
    session: Session,
    *,
    start: date | None = None,
    end: date | None = None,
    counterparty: str | None = None,
    category: str | None = None,
    direction: str = "out",
) -> Totals:
    """
    What was spent in a window, per currency.

    `counterparty` is a case-insensitive substring: a ledger holds "MERCADONA
    S.A." and a person asks about "mercadona".
    """
    stmt = _window(_live(direction), start, end)
    if counterparty:
        stmt = stmt.where(Transaction.counterparty.ilike(f"%{counterparty}%"))
    if category:
        stmt = stmt.where(Transaction.category == category)

    rows = list(session.scalars(stmt))
    return Totals(
        amounts=_by_currency(rows, "amount"),
        tax=_by_currency(rows, "tax_amount"),
        document_ids=[r.document_id for r in rows],
    )


def list_transactions(
    session: Session,
    *,
    start: date | None = None,
    end: date | None = None,
    counterparty: str | None = None,
    min_amount: Decimal | None = None,
    direction: str = "out",
    limit: int = DEFAULT_LIMIT,
) -> list[dict]:
    """Individual rows, newest first, each with the document behind it."""
    stmt = _window(_live(direction), start, end)
    if counterparty:
        stmt = stmt.where(Transaction.counterparty.ilike(f"%{counterparty}%"))
    if min_amount is not None:
        stmt = stmt.where(Transaction.amount >= min_amount)

    stmt = stmt.order_by(Transaction.occurred_on.desc().nullslast()).limit(
        max(1, min(limit, MAX_LIMIT))
    )
    return [
        {
            "transaction_id": r.id,
            "document_id": r.document_id,
            "occurred_on": r.occurred_on.isoformat() if r.occurred_on else None,
            "counterparty": r.counterparty,
            "amount": amount_str(r.amount),
            "tax_amount": (
                amount_str(r.tax_amount) if r.tax_amount is not None else None
            ),
            "currency": r.currency,
            "category": r.category,
            "user_verified": r.user_verified,
            "derived": r.derived_fields or [],
        }
        for r in session.scalars(stmt)
    ]


def spend_by_counterparty(
    session: Session,
    *,
    start: date | None = None,
    end: date | None = None,
    direction: str = "out",
    limit: int = 10,
) -> list[dict]:
    """
    Who the money went to, biggest first.

    Grouped in SQL rather than by pulling rows and adding them up, and split
    by currency for the same reason totals are.
    """
    stmt = (
        _window(_live(direction), start, end)
        .with_only_columns(
            Transaction.counterparty,
            Transaction.currency,
            func.sum(Transaction.amount).label("total"),
            func.count(Transaction.id).label("n"),
        )
        .group_by(Transaction.counterparty, Transaction.currency)
        .order_by(func.sum(Transaction.amount).desc())
        .limit(max(1, min(limit, MAX_LIMIT)))
    )
    return [
        {
            "counterparty": row.counterparty,
            "currency": row.currency or "unstated",
            "total": amount_str(row.total),
            "transactions": row.n,
        }
        for row in session.execute(stmt)
    ]


def spend_by_month(
    session: Session,
    *,
    start: date | None = None,
    end: date | None = None,
    direction: str = "out",
) -> list[dict]:
    """Monthly totals, oldest first. Rows with no date are reported separately."""
    rows = list(session.scalars(_window(_live(direction), start, end)))

    buckets: dict[str, dict[str | None, list[Decimal]]] = {}
    for row in rows:
        key = row.occurred_on.strftime("%Y-%m") if row.occurred_on else "undated"
        buckets.setdefault(key, {}).setdefault(row.currency, []).append(row.amount)

    out = []
    for month in sorted(buckets):
        for currency, values in sorted(buckets[month].items(), key=lambda kv: (kv[0] or "",)):
            out.append(
                {
                    "month": month,
                    "currency": currency or "unstated",
                    "total": amount_str(sum(values, Decimal("0"))),
                    "transactions": len(values),
                }
            )
    return out


def get_source(session: Session, transaction_id: str) -> dict | None:
    """
    The evidence behind one ledger row.

    This is what makes a citation worth anything: the answer can be traced to
    the extraction that produced it and the document that was read, right
    down to the OCR text somebody can check by eye.
    """
    # Guarded rather than handed straight to session.get: a null primary key
    # warns today ("fully NULL primary key identity cannot load any object")
    # and SQLAlchemy says it may raise in a later release. A model passing an
    # empty id should get "no such transaction", not a deprecation.
    if not transaction_id:
        return None
    transaction = session.get(Transaction, transaction_id)
    if transaction is None:
        return None

    extraction = session.get(Extraction, transaction.source_extraction_id)
    document = session.get(Document, transaction.document_id)

    return {
        "transaction_id": transaction.id,
        # Also at the top level, matching list_transactions. The nested
        # document block is for a reader; this is what citation collection
        # looks for, and burying it cost a drill-down its source.
        "document_id": transaction.document_id,
        "amount": amount_str(transaction.amount),
        "currency": transaction.currency,
        "occurred_on": (
            transaction.occurred_on.isoformat() if transaction.occurred_on else None
        ),
        "counterparty": transaction.counterparty,
        # Columns a transform worked out rather than the model reading them.
        # Without this, a computed total of 120.00 sat beside a raw_json
        # saying `total: null` -- evidence that seemed to contradict the row.
        "derived": transaction.derived_fields or [],
        "document": {
            "id": document.id if document else None,
            "filename": document.original_filename if document else None,
            "uploaded_at": (
                document.uploaded_at.isoformat() if document and document.uploaded_at else None
            ),
        },
        "extraction": {
            "doc_type": extraction.doc_type if extraction else None,
            "schema_version": extraction.schema_version if extraction else None,
            "model": extraction.model_used if extraction else None,
            # What the model read, and what the type's transform made of it.
            # Both, because the first is the evidence and the second is what
            # the ledger row was built from.
            "raw_json": extraction.raw_json if extraction else None,
            "transformed_json": extraction.transformed_json if extraction else None,
        },
    }


def ledger_summary(session: Session) -> dict:
    """
    What is in the ledger at all. The orienting call, so the agent does not
    guess at date ranges or invent counterparties that were never there.
    """
    stmt = _live(direction=None).with_only_columns(
        func.count(Transaction.id),
        func.min(Transaction.occurred_on),
        func.max(Transaction.occurred_on),
    )
    count, earliest, latest = session.execute(stmt).one()
    currencies = list(
        session.scalars(
            _live(direction=None)
            .with_only_columns(Transaction.currency)
            .distinct()
        )
    )
    return {
        "transactions": count or 0,
        "earliest": earliest.isoformat() if earliest else None,
        "latest": latest.isoformat() if latest else None,
        "currencies": sorted(c or "unstated" for c in currencies),
    }
