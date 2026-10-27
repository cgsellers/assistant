"""
Turning a validated extraction into a ledger row.

This is the last deterministic step, and the one that decides what the
analyst agent will ever be able to see. It reads the `canonical` block of a
document type, so adding a type that reaches the ledger is still a row in
`document_types` rather than a module -- the same argument as 5.20, applied
one layer further down.

Nothing here infers. A value the document did not carry stays null, which is
why the ledger can be trusted in a way the raw JSON cannot: every figure in
it was read off a page.
"""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal
from typing import Any

from doctypes.definition import Definition

#: ISO 4217: three letters, nothing else. A document type whose currency
#: field returns "Euros" is misconfigured, and writing that into a ledger
#: column is worse than leaving it null -- the raw value is still in
#: `extractions.raw_json` either way.
_CURRENCY = re.compile(r"^[A-Za-z]{3}$")


def to_ledger(definition: Definition, data: dict) -> tuple[dict | None, str | None]:
    """
    Canonical column values from coerced extraction data.

    Returns `(values, None)` when a row can be built, or `(None, reason)`
    when it cannot. A reason is not an error: a document type with no
    `canonical` block is extracted and validated and simply stops short of
    the ledger, which is the right behaviour for a type still being shaped.

    `data` must already be coerced -- amounts as Decimal, dates as date --
    because the canonical columns are typed and the raw JSON is not.
    """
    mapping = definition.canonical
    if mapping is None:
        return None, f"{definition.name} has no canonical mapping"

    amount = data.get(mapping.amount)
    if amount is None:
        # Validation should normally have caught this, but a type is free not
        # to declare a `required` rule, and a ledger row with no amount is
        # not a transaction.
        return None, f"{mapping.amount} is null, so there is no amount to record"
    if not isinstance(amount, Decimal):
        return None, f"{mapping.amount} is not an amount ({type(amount).__name__})"

    values: dict[str, Any] = {
        "source_type": definition.name,
        "direction": str(mapping.direction),
        "amount": amount,
    }

    if mapping.tax_amount:
        tax = data.get(mapping.tax_amount)
        values["tax_amount"] = tax if isinstance(tax, Decimal) else None

    if values.get("tax_amount") is None and mapping.tax_lines:
        values["tax_amount"] = _sum_taxes(data.get(mapping.tax_lines))

    if mapping.occurred_on:
        occurred = data.get(mapping.occurred_on)
        values["occurred_on"] = occurred if isinstance(occurred, date) else None

    if mapping.counterparty:
        values["counterparty"] = _text(data.get(mapping.counterparty), limit=200)

    if mapping.counterparty_tax_id:
        values["counterparty_tax_id"] = _text(
            data.get(mapping.counterparty_tax_id), limit=50
        )

    if mapping.currency:
        values["currency"] = _currency(data.get(mapping.currency))

    return values, None


def to_summary(definition: Definition, data: dict) -> dict[str, Any]:
    """
    What the documents list shows for a document: who issued it, its own date,
    its total. Built the moment it is read, whether or not it passes its rules.

    The same mapping as `to_ledger`, but forgiving where that is strict. The
    ledger refuses a row with no amount, rightly; the list wants exactly the
    documents that have problems, showing what was read and leaving a gap where
    nothing was. So a missing or unreadable value is simply None here.
    """
    summary: dict[str, Any] = {
        "doc_type": definition.name,
        "supplier": None,
        "document_date": None,
        "total_amount": None,
        "total_currency": None,
    }
    mapping = definition.canonical
    if mapping is None:
        return summary
    amount = data.get(mapping.amount)
    summary["total_amount"] = amount if isinstance(amount, Decimal) else None
    if mapping.occurred_on:
        occurred = data.get(mapping.occurred_on)
        summary["document_date"] = occurred if isinstance(occurred, date) else None
    if mapping.counterparty:
        summary["supplier"] = _text(data.get(mapping.counterparty), limit=200)
    if mapping.currency:
        summary["total_currency"] = _currency(data.get(mapping.currency))
    return summary


def derived_columns(definition: Definition, data: dict, derived: list[str]) -> list[str]:
    """
    The ledger columns a transform worked out, given the fields it changed.

    This is what keeps the module's promise honest once transforms exist: a
    column listed here was not read off a page, and the row says so.
    """
    mapping = definition.canonical
    if mapping is None or not derived:
        return []
    changed = set(derived)
    columns = [
        column for column, name in mapping.referenced().items()
        if name in changed and column != "tax_lines"
    ]
    # The ledger's tax comes from the tax lines when no single figure was
    # printed, so derived tax lines mean a derived tax.
    fell_back = mapping.tax_amount is None or data.get(mapping.tax_amount) is None
    if mapping.tax_lines in changed and fell_back and "tax_amount" not in columns:
        columns.append("tax_amount")
    return columns


def _sum_taxes(lines: Any) -> Decimal | None:
    """
    The total of separately printed taxes -- added here, in code, because the
    model is told never to add anything up. All or nothing: if any line's
    amount is unreadable the total is unknown, not smaller, so it is null.
    """
    if not isinstance(lines, list) or not lines:
        return None
    amounts = [line.get("amount") if isinstance(line, dict) else None for line in lines]
    if not all(isinstance(a, Decimal) for a in amounts):
        return None
    return sum(amounts, Decimal("0"))


def _text(value: Any, *, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = re.sub(r"\s+", " ", value).strip()
    return cleaned[:limit] or None


def _currency(value: Any) -> str | None:
    """Uppercased ISO code, or null. Never a guess at what was meant."""
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned.upper() if _CURRENCY.match(cleaned) else None
