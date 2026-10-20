"""
One rule for what a money value looks like.

The same amount had three spellings in this codebase. `Numeric(14, 4)` reads
back `10.0000` where the mapper wrote `10.00`; the analyst quantised to two
places; the worker called `str()` and got whichever it happened to hold. So
`document_events` recorded an amount of `"9.35"` for a ledger row holding
`9.3500` -- identical numbers, and an audit trail that does not obviously
match the table it describes.

None of that is a rounding error. It is three modules each deciding
separately how to render a Decimal, which is the kind of thing that stays
invisible until something compares two of them.

The rule: **two decimal places, unless the value genuinely carries more.**
Trailing zeros past two places are dropped rather than the value being
rounded, so a three-decimal currency survives intact.
"""

from __future__ import annotations

from decimal import Decimal

CENTS = Decimal("0.01")


def normalise(value: Decimal) -> Decimal:
    """
    The canonical form of an amount.

    `Decimal("10.0000")` and `Decimal("10")` both become `Decimal("10.00")`.
    `Decimal("10.1230")` becomes `Decimal("10.123")`, because that third
    place is a real digit somebody wrote down.
    """
    trimmed = value.normalize()
    places = -trimmed.as_tuple().exponent
    return value.quantize(CENTS) if places <= 2 else trimmed


def to_str(value: Decimal) -> str:
    """An amount as it should appear anywhere a human or a model reads it."""
    return str(normalise(value))
