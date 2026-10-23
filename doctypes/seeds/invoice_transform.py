"""
The invoice transform: fill in a total the invoice did not print.

The model is told never to compute anything, so an invoice with a subtotal
and tax but no total line comes back with the total null. Working it out is
arithmetic, which is code's job. Whatever this fills in is recorded as
derived, on the extraction and on the ledger row.

  * Total printed: left alone, whatever else is missing.
  * No total, subtotal printed: subtotal - discount + tax. The tax is the
    printed tax_amount, else the sum of the printed tax lines, else none.
  * No total and no subtotal: left null, and the `required` rule sends the
    invoice to review.

Runs in its own interpreter with the standard library only: it receives the
extraction as JSON, with money as strings like "143.39".
"""

from decimal import Decimal


def transform(data):
    if data["total"] is not None or data["subtotal"] is None:
        return data

    tax = _tax(data)
    if tax is None:
        return data

    total = Decimal(data["subtotal"]) - Decimal(data["discount"] or "0") + tax
    return {**data, "total": str(total)}


def _tax(data):
    """The tax to add, or None if a printed tax line has no readable amount."""
    if data["tax_amount"] is not None:
        return Decimal(data["tax_amount"])
    lines = data["taxes"] or []
    if any(line["amount"] is None for line in lines):
        return None
    return sum((Decimal(line["amount"]) for line in lines), Decimal("0"))
