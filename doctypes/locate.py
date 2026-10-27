"""
Where on the original each value was read, for the portal to highlight
(DESIGN §9.4).

The model returns values, not positions, so positions are found afterwards:
each value is looked for in the OCR's words, in the forms a document prints
it -- "1.234,56 €" for 1234.56, "24/10/2026" or "Oct 24, 2026" for
2026-10-24 -- and its box becomes fractions of its page, which hold at any
zoom.

A wrong highlight is worse than none, so this would rather find nothing:
short numbers (a quantity of 1 appears everywhere) are not looked for, and
when a value appears more than once, the line carrying the field's label
wins -- TOTAL for the total, the words its processor's description shouts in
capitals. Nothing found is a None, which the portal shows as nothing at all.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from doctypes.definition import Definition, FieldSpec, FieldType

Box = tuple[float, float, float, float]


@dataclass
class Line:
    """Words side by side on one page, as a person reads them."""

    page: int
    words: list[tuple[str, Box]] = field(default_factory=list)
    text: str = ""
    #: Each word's character span in `text`.
    spans: list[tuple[int, int]] = field(default_factory=list)

    @property
    def top(self) -> float:
        return min(b[1] for _, b in self.words)

    def box(self, start: int = 0, end: int | None = None) -> Box:
        """The box around the words that `text[start:end]` touches."""
        end = len(self.text) if end is None else end
        boxes = [
            b
            for (_, b), (s, e) in zip(self.words, self.spans, strict=True)
            if s < end and e > start
        ]
        return (
            min(b[0] for b in boxes),
            min(b[1] for b in boxes),
            max(b[2] for b in boxes),
            max(b[3] for b in boxes),
        )


def lines_of(blocks: list[dict]) -> list[Line]:
    """
    Group OCR blocks into lines: same page, overlapping vertically by at least
    half the shorter one's height. Works for engines that return words
    (Tesseract) and those that return whole lines (PP-OCR, PDF text layers).
    """
    items = sorted(
        (
            (int(b.get("page", 1)), (b["bbox"][1] + b["bbox"][3]) / 2, b)
            for b in blocks
            if b.get("text") and b.get("bbox")
        ),
        key=lambda item: (item[0], item[1]),
    )
    lines: list[Line] = []
    for page, _, block in items:
        x0, y0, x1, y1 = (float(v) for v in block["bbox"])
        current = lines[-1] if lines and lines[-1].page == page else None
        if current is not None:
            top = min(b[1] for _, b in current.words)
            bottom = max(b[3] for _, b in current.words)
            overlap = min(bottom, y1) - max(top, y0)
            if overlap >= 0.5 * min(bottom - top, y1 - y0):
                current.words.append((str(block["text"]), (x0, y0, x1, y1)))
                continue
        lines.append(Line(page=page, words=[(str(block["text"]), (x0, y0, x1, y1))]))
    for line in lines:
        line.words.sort(key=lambda w: w[1][0])
        parts, spans, at = [], [], 0
        for text, _ in line.words:
            spans.append((at, at + len(text)))
            parts.append(text)
            at += len(text) + 1
        line.text, line.spans = " ".join(parts), spans
    return lines


# --- Reading values the way documents print them -----------------------------------

_NUMBER = re.compile(r"-?\d[\d.,']*\d|\d")


def amounts_in(token: str) -> set[Decimal]:
    """
    Every amount a printed number could be. "1.234,56" and "1,234.56" are both
    1234.56; "1,234" could be 1234 or 1.234 and is both, since this only asks
    whether a known value was printed, never what an unknown one means.
    """
    t = token.replace("'", "")
    if "." in t and "," in t:
        decimal_mark = "." if t.rfind(".") > t.rfind(",") else ","
        grouping = "," if decimal_mark == "." else "."
        readings = [t.replace(grouping, "").replace(decimal_mark, ".")]
    elif "," in t or "." in t:
        mark = "," if "," in t else "."
        readings = [t.replace(mark, "")]
        if t.count(mark) == 1:
            readings.append(t.replace(mark, "."))
    else:
        readings = [t]
    found = set()
    for reading in readings:
        try:
            found.add(Decimal(reading))
        except InvalidOperation:
            continue
    return found


_MONTHS: dict[str, int] = {}
for _number, _names in enumerate(
    [
        "jan ene gen janv",
        "feb fév fev févr",
        "mar mrt mär",
        "apr abr avr",
        "may mai mag mei",
        "jun giu juin",
        "jul lug juil",
        "aug ago aoû aou",
        "sep set",
        "oct ott okt",
        "nov",
        "dec dic dez déc",
    ],
    start=1,
):
    for _name in _names.split():
        _MONTHS[_name] = _number

_NUMERIC_DATE = re.compile(r"(?<!\d)(\d{1,4})[./-](\d{1,2})[./-](\d{1,4})(?!\d)")
# "24 Oct 2026", "24th October 2026", and Spanish "24 de octubre de 2026".
_DAY_MONTH_YEAR = re.compile(
    r"(?<!\d)(\d{1,2})(?:st|nd|rd|th)?\.?\s+(?:de\s+)?([^\W\d_]{3,})\.?,?\s+(?:de\s+)?(\d{2,4})(?!\d)"
)
_MONTH_DAY_YEAR = re.compile(
    r"(?<![^\W\d_])([^\W\d_]{3,})\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})(?!\d)"
)


def _month(name: str) -> int | None:
    name = name.lower()
    return _MONTHS.get(name[:4]) or _MONTHS.get(name[:3])


def _year(text: str) -> int:
    year = int(text)
    return year + 2000 if year < 100 else year


def _real(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def dates_in(text: str) -> list[tuple[int, int, set[date]]]:
    """Every date a line could be printing, with its span; read day-first and month-first."""
    found = []
    for m in _NUMERIC_DATE.finditer(text):
        a, b, c = m.groups()
        readings: set[date | None]
        if len(a) == 4:
            readings = {_real(int(a), int(b), int(c))}
        else:
            readings = {
                _real(_year(c), int(b), int(a)),
                _real(_year(c), int(a), int(b)),
            }
        found.append((m.start(), m.end(), {d for d in readings if d}))
    for m in _DAY_MONTH_YEAR.finditer(text):
        day, name, year = m.groups()
        month = _month(name)
        if month and (when := _real(_year(year), month, int(day))):
            found.append((m.start(), m.end(), {when}))
    for m in _MONTH_DAY_YEAR.finditer(text):
        name, day, year = m.groups()
        month = _month(name)
        if month and (when := _real(int(year), month, int(day))):
            found.append((m.start(), m.end(), {when}))
    return found


# --- Finding one value ----------------------------------------------------------------

_HINT_STOPWORDS = {"the", "and", "for", "from", "with", "not", "this", "that", "value"}


def hints(name: str, spec: FieldSpec) -> set[str]:
    """
    Words that mark the line a value sits on: the field's own name, and the
    words its description writes in capitals -- "labelled TOTAL", "CIF or NIF".
    """
    words = {w for w in name.lower().split("_") if len(w) >= 3}
    words |= {w.lower() for w in re.findall(r"\b[A-ZÀ-Ý]{3,}\b", spec.description)}
    return words - _HINT_STOPWORDS


def _score(line: Line, words: set[str]) -> int:
    text = line.text.lower()
    return sum(1 for w in words if re.search(rf"(?<![^\W_]){re.escape(w)}(?![^\W_])", text))


@dataclass(frozen=True)
class _Hit:
    line: Line
    start: int
    end: int


def _hits(value: Any, kind: FieldType, lines: list[Line]) -> list[_Hit]:
    hits: list[_Hit] = []
    if kind in (FieldType.MONEY, FieldType.NUMBER) and value is not None:
        try:
            wanted = Decimal(str(value))
        except InvalidOperation:
            return []
        # A small number is printed everywhere: quantities, page numbers.
        if abs(wanted) < 10 and wanted == wanted.to_integral_value():
            return []
        for line in lines:
            for m in _NUMBER.finditer(line.text):
                if wanted in amounts_in(m.group()):
                    hits.append(_Hit(line, m.start(), m.end()))
    elif kind == FieldType.DATE and value:
        try:
            wanted_day = date.fromisoformat(str(value))
        except ValueError:
            return []
        for line in lines:
            for start, end, readings in dates_in(line.text):
                if wanted_day in readings:
                    hits.append(_Hit(line, start, end))
    elif kind == FieldType.STRING and isinstance(value, str):
        needle = " ".join(value.split()).lower()
        if len(needle) < 3:
            return []
        candidates = [needle]
        # A value printed over several lines, such as an address, is found by
        # its first line.
        if "," in needle and len(first := needle.split(",")[0].strip()) >= 4:
            candidates.append(first)
        for candidate in candidates:
            for line in lines:
                at = line.text.lower().find(candidate)
                if at != -1:
                    hits.append(_Hit(line, at, at + len(candidate)))
            if hits:
                break
    return hits


def _best(hits: list[_Hit], words: set[str], kind: FieldType) -> _Hit | None:
    if not hits:
        return None
    # The labelled line first. Among equals, an amount is taken from the
    # bottom (the total comes after the lines it totals), anything else from
    # the top (headers come first).
    lowest = kind == FieldType.MONEY
    return max(
        hits,
        key=lambda h: (
            _score(h.line, words),
            (h.line.page, h.line.top) if lowest else (-h.line.page, -h.line.top),
        ),
    )


# --- Places on the page ---------------------------------------------------------------


def _place(box: Box, page: int, sizes: dict[int, tuple[float, float]]) -> dict | None:
    """A box in OCR units as fractions of its page, padded; None without the page's size."""
    if page not in sizes:
        return None
    width, height = sizes[page]
    pad_x, pad_y = width * 0.004, height * 0.004
    x0, y0, x1, y1 = box
    return {
        "page": page,
        "box": [
            max(0.0, (x0 - pad_x) / width),
            max(0.0, (y0 - pad_y) / height),
            min(1.0, (x1 + pad_x) / width),
            min(1.0, (y1 + pad_y) / height),
        ],
    }


def locate(
    definition: Definition,
    data: dict[str, Any],
    blocks: list[dict] | None,
    page_sizes: list[dict] | None,
) -> dict[str, dict | None]:
    """
    Where each value in `data` was printed: by field name, and by "list.row"
    for each row of a list. Missing keys mean "not found"; the whole map is
    empty when the OCR recorded no page sizes (read before it did).
    """
    sizes = {int(p["page"]): (float(p["width"]), float(p["height"])) for p in page_sizes or []}
    if not sizes or not blocks:
        return {}
    lines = lines_of(blocks)
    found: dict[str, dict | None] = {}
    for name, spec in definition.fields.items():
        value = data.get(name)
        if spec.type is FieldType.LIST:
            found.update(_rows(name, spec, value, lines, sizes))
            continue
        if spec.type in (FieldType.BOOLEAN, FieldType.INTEGER):
            continue
        hit = _best(_hits(value, spec.type, lines), hints(name, spec), spec.type)
        if hit is not None:
            found[name] = _place(hit.line.box(hit.start, hit.end), hit.line.page, sizes)
    return found


def _rows(
    name: str, spec: FieldSpec, rows: Any, lines: list[Line], sizes: dict[int, tuple[float, float]]
) -> dict[str, dict | None]:
    """
    Each row of a list as the whole line it was printed on: the line where most
    of the row's text and amounts appear. A row's name alone misleads -- a tax
    row called "VAT" also matches "VAT No GB987654321" in the header -- while
    "VAT 20% 17.48" carries its amount too. Each line goes to one row at most.
    """
    if not isinstance(rows, list) or not spec.item_fields:
        return {}
    columns = [
        (c, s.type)
        for c, s in spec.item_fields.items()
        if s.type in (FieldType.STRING, FieldType.MONEY)
    ]
    used: set[int] = set()
    found: dict[str, dict | None] = {}
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            continue
        # For each line, which of this row's values it carries.
        carried: dict[int, tuple[int, Line, set[str]]] = {}
        for column, kind in columns:
            for hit in _hits(row.get(column), kind, lines):
                if id(hit.line) in used:
                    continue
                order, line, matched = carried.get(
                    id(hit.line), (lines.index(hit.line), hit.line, set())
                )
                matched.add(column)
                carried[id(hit.line)] = (order, line, matched)
        if not carried:
            continue
        # Most values first; among equals, the line printed first.
        _, line, _ = max(carried.values(), key=lambda c: (len(c[2]), -c[0]))
        used.add(id(line))
        found[f"{name}.{index}"] = _place(line.box(), line.page, sizes)
    return found
