"""
Synthetic invoices and receipts for the demo: generated PDFs, and what a
model would read from each.

Every name, address and number is invented. Each supplier prints the way its
country does -- "€ 1.234,56" in Rotterdam, "10/24/2026" in Seattle, "12,50 €"
in Barcelona -- so the portal shows what normalising does. Each document also
carries the answer a model would give for it, which is what lets the demo be
read through the real pipeline without a single paid call: OCR, routing,
transform, rules and ledger all run; only the model's answer is known in
advance.

PDFs are written by hand: one page, the PDF's built-in Helvetica and Courier
(nothing embedded), a real text layer, which the pipeline reads locally.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

CENT = Decimal("0.01")

# --- Printing in the supplier's own style ----------------------------------------------


def _grouped(amount: Decimal, thousands: str, decimal_mark: str) -> str:
    sign = "-" if amount < 0 else ""
    whole, _, fraction = f"{abs(amount):.2f}".partition(".")
    groups = []
    while len(whole) > 3:
        groups.insert(0, whole[-3:])
        whole = whole[:-3]
    groups.insert(0, whole)
    return f"{sign}{thousands.join(groups)}{decimal_mark}{fraction}"


@dataclass(frozen=True)
class Style:
    """How a supplier prints numbers, money and dates."""

    thousands: str
    decimal_mark: str
    #: "{symbol}{n}", "{symbol} {n}" or "{n} {symbol}".
    money: str
    #: "dmy-", "dmy/", "mdy/" or "d mon y".
    dates: str

    def number(self, amount: Decimal) -> str:
        return _grouped(amount, self.thousands, self.decimal_mark)

    def amount(self, amount: Decimal, symbol: str) -> str:
        return self.money.format(symbol=symbol, n=self.number(amount))

    def day(self, when: date) -> str:
        if self.dates == "mdy/":
            return f"{when.month:02d}/{when.day:02d}/{when.year}"
        if self.dates == "dmy/":
            return f"{when.day:02d}/{when.month:02d}/{when.year}"
        if self.dates == "dmy-":
            return f"{when.day:02d}-{when.month:02d}-{when.year}"
        return when.strftime("%-d %b %Y")


UK = Style(",", ".", "{symbol}{n}", "d mon y")
US = Style(",", ".", "{symbol}{n}", "mdy/")
NL = Style(".", ",", "{symbol} {n}", "dmy-")
ES = Style(".", ",", "{n} {symbol}", "dmy/")

SYMBOL = {"GBP": "£", "EUR": "€", "USD": "$"}


@dataclass(frozen=True)
class Supplier:
    name: str
    address: tuple[str, str]
    tax_id: str
    currency: str
    style: Style
    tax_name: str
    tax_rate: Decimal
    items: tuple[str, ...]
    colour: tuple[float, float, float] = (0.2, 0.25, 0.55)
    payment: str = ""


INVOICERS = (
    Supplier(
        "Northwind Office Supply", ("14 Canal Street", "Manchester M1 3HE"), "GB284193706",
        "GBP", UK, "VAT", Decimal("20"),
        ("A4 copier paper, 5 reams", "Toner cartridge, black", "Desk organiser",
         "Whiteboard markers"),
        (0.13, 0.36, 0.62),
    ),
    Supplier(
        "Globe Logistics BV", ("Waalhaven Oostzijde 41", "3087 BM Rotterdam"), "NL853127604B01",
        "EUR", NL, "BTW", Decimal("21"),
        ("Pallet freight Rotterdam-London", "Fuel surcharge", "Customs clearance",
         "Storage, 7 days"),
        (0.85, 0.42, 0.11),
    ),
    Supplier(
        "Contoso Cloud Inc.", ("500 Pine Street, Suite 1200", "Seattle, WA 98101"), "91-4827365",
        "USD", US, "Sales tax", Decimal("0"),
        ("Compute, standard tier", "Object storage", "Support plan", "Data transfer"),
        (0.2, 0.2, 0.55),
    ),
    Supplier(
        "Fabrikam Furniture Ltd", ("Unit 7, Riverside Park", "Leeds LS10 1RJ"), "GB731440218",
        "GBP", UK, "VAT", Decimal("20"),
        ("Ergonomic chair", "Standing desk frame", "Delivery and assembly", "Monitor arm"),
        (0.18, 0.45, 0.3),
    ),
)  # fmt: skip

SHOPS = (
    Supplier(
        "CAFE LUNA", ("Carrer de Mallorca 210", "08008 Barcelona"), "B12345678",
        "EUR", ES, "IVA", Decimal("10"),
        ("Cafe con leche", "Croissant", "Zumo de naranja", "Tostada con tomate"), payment="Tarjeta",
    ),
    Supplier(
        "FARMACIA SOL", ("Avinguda Diagonal 455", "08036 Barcelona"), "B87654321",
        "EUR", ES, "IVA", Decimal("4"),
        ("Ibuprofeno 400mg", "Crema solar", "Tiritas", "Suero fisiologico"), payment="Efectivo",
    ),
    Supplier(
        "TAXI BCN 2741", ("Licencia 2741", "Barcelona"), "38291047K",
        "EUR", ES, "IVA", Decimal("10"),
        ("Carrera aeropuerto", "Suplemento equipaje"), payment="Tarjeta",
    ),
    # Italian: its P.IVA fails the receipt processor's Spanish CIF/NIF rule --
    # a rule too narrow for the document, which is what "approve as it is" is for.
    Supplier(
        "COOP ITALIA", ("Via Roma 12", "20121 Milano"), "01234567890",
        "EUR", ES, "IVA", Decimal("10"),
        ("Pane integrale", "Latte intero 1L", "Mozzarella", "Caffe macinato"), payment="Carta",
    ),
)  # fmt: skip

BILL_TO = ("Acme Ltd", "1 Market Street", "London EC2A 4NE")


# --- A PDF, by hand ---------------------------------------------------------------------

#: Helvetica's advance widths for ASCII 32-126, in thousandths of the font size.
_HELVETICA = (
    278, 278, 355, 556, 556, 889, 667, 191, 333, 333, 389, 584, 278, 333, 278, 278, 556, 556,
    556, 556, 556, 556, 556, 556, 556, 556, 278, 278, 584, 584, 584, 556, 1015, 667, 667, 722,
    722, 667, 611, 778, 722, 278, 500, 667, 556, 833, 722, 778, 667, 778, 722, 667, 611, 722,
    667, 944, 667, 667, 611, 278, 278, 278, 469, 556, 333, 556, 556, 500, 556, 556, 278, 556,
    556, 222, 222, 500, 222, 833, 556, 556, 556, 556, 333, 500, 278, 556, 500, 722, 500, 500,
    500, 334, 260, 334, 584,
)  # fmt: skip

#: WinAnsi codes for the characters outside ASCII that suppliers print.
_WIN_ANSI = {"€": 0x80, "£": 0xA3, "–": 0x96}


def _pdf_string(text: str) -> str:
    out = []
    for char in text:
        code = ord(char)
        if char in "()\\":
            out.append("\\" + char)
        elif 32 <= code < 127:
            out.append(char)
        else:
            out.append(f"\\{_WIN_ANSI.get(char, code if 160 <= code < 256 else 63):03o}")
    return "(" + "".join(out) + ")"


class Page:
    def __init__(self, width: float = 595, height: float = 842, *, mono: bool = False):
        self.width, self.height, self.mono = width, height, mono
        self.ops: list[str] = []

    def measure(self, text: str, size: float, bold: bool) -> float:
        if self.mono:
            return len(text) * size * 0.6
        total = sum(_HELVETICA[ord(c) - 32] if 32 <= ord(c) < 127 else 556 for c in text)
        return total * size * (1.1 if bold else 1) / 1000

    def text(self, x, y, text, *, size=10.0, bold=False, grey=0.13, align="left"):
        width = self.measure(text, size, bold)
        left = x - width if align == "right" else x - width / 2 if align == "center" else x
        font = ("F4" if bold else "F3") if self.mono else ("F2" if bold else "F1")
        self.ops.append(
            f"BT /{font} {size} Tf {grey} {grey} {grey} rg "
            f"{left:.2f} {y:.2f} Td {_pdf_string(text)} Tj ET"
        )

    def fill(self, x, y, w, h, rgb):
        r, g, b = rgb
        self.ops.append(f"{r} {g} {b} rg {x} {y} {w} {h} re f")

    def rule(self, x1, y1, x2, y2, grey=0.85):
        self.ops.append(f"{grey} G 0.6 w {x1} {y1} m {x2} {y2} l S")

    def pdf(self) -> bytes:
        content = "\n".join(self.ops)
        fonts = " ".join(
            f"/{name} << /Type /Font /Subtype /Type1 /BaseFont /{base} "
            "/Encoding /WinAnsiEncoding >>"
            for name, base in (
                ("F1", "Helvetica"),
                ("F2", "Helvetica-Bold"),
                ("F3", "Courier"),
                ("F4", "Courier-Bold"),
            )
        )
        objects = [
            "<< /Type /Catalog /Pages 2 0 R >>",
            "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {self.width} {self.height}] "
            f"/Resources << /Font << {fonts} >> >> /Contents 4 0 R >>",
            f"<< /Length {len(content)} >>\nstream\n{content}\nendstream",
        ]
        # ASCII throughout (strings are escaped), so characters are bytes.
        out = "%PDF-1.4\n"
        offsets = []
        for number, body in enumerate(objects, start=1):
            offsets.append(len(out))
            out += f"{number} 0 obj\n{body}\nendobj\n"
        xref = len(out)
        out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n"
        out += "".join(f"{offset:010d} 00000 n \n" for offset in offsets)
        out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n"
        return out.encode("ascii")


# --- The documents -----------------------------------------------------------------------


@dataclass
class Line:
    description: str
    quantity: int
    unit: Decimal

    @property
    def amount(self) -> Decimal:
        return (self.unit * self.quantity).quantize(CENT)


@dataclass
class Synthetic:
    """One generated document: its file, what a model would read, and when it arrived."""

    key: str
    filename: str
    pdf: bytes
    #: The model's answer, in its wire form; None for a document nobody would read.
    answer: dict[str, Any] | None
    received: date
    #: What the demo shows it for, in a sentence.
    shows: str
    extra: dict[str, Any] = field(default_factory=dict)


def _lines(rng: random.Random, items: tuple[str, ...], low: int, high: int) -> list[Line]:
    chosen = rng.sample(items, k=min(len(items), rng.randint(2, 3)))
    return [
        Line(name, rng.choice((1, 1, 2, 3)), Decimal(rng.randint(low, high)) / 100)
        for name in chosen
    ]


def invoice(
    rng: random.Random, supplier: Supplier, number: int, issued: date, *, variant: str = "clean"
) -> Synthetic:
    """
    An invoice. Variants: "clean"; "no_total" (prints no total, which the
    invoice transform works out); "cut_off" (page 1 of 2: no totals at all);
    "credit" (a credit note: a negative total, which the rules refuse).
    """
    lines = _lines(rng, supplier.items, 2_000, 60_000)
    if variant == "credit":
        lines = [Line(f"Credit: {lines[0].description}", 1, -lines[0].unit)]
    subtotal = sum((line.amount for line in lines), Decimal("0"))
    tax = (subtotal * supplier.tax_rate / 100).quantize(CENT, ROUND_HALF_UP)
    total = subtotal + tax
    due = issued + timedelta(days=30)
    ref = f"INV-{issued.year}-{number:04d}"
    po = f"PO-{31000 + number * 7}"
    s, sym = supplier.style, SYMBOL[supplier.currency]

    page = Page()
    left, right = 50, 545
    page.fill(0, 822, 595, 20, supplier.colour)
    page.text(left, 772, supplier.name, size=18, bold=True)
    page.text(left, 754, supplier.address[0], size=9, grey=0.45)
    page.text(left, 741, supplier.address[1], size=9, grey=0.45)
    page.text(left, 728, f"{supplier.tax_name} no. {supplier.tax_id}", size=9, grey=0.45)
    title = "CREDIT NOTE" if variant == "credit" else "INVOICE"
    page.text(right, 772, title, size=20, bold=True, align="right")
    meta = (
        ("Invoice no.", ref),
        ("Invoice date", s.day(issued)),
        ("Due date", s.day(due)),
        ("PO number", po),
    )
    for i, (label, value) in enumerate(meta):
        page.text(380, 748 - i * 14, label, size=9, grey=0.45)
        page.text(right, 748 - i * 14, value, size=9, align="right")
    page.text(left, 690, "BILL TO", size=8, bold=True, grey=0.45)
    for i, line in enumerate(BILL_TO):
        page.text(left, 675 - i * 14, line, size=10, bold=i == 0)

    y = 600
    page.fill(left, y - 7, right - left, 22, (0.95, 0.95, 0.96))
    page.text(left + 8, y, "Description", size=9, bold=True)
    page.text(370, y, "Qty", size=9, bold=True, align="right")
    page.text(455, y, "Unit price", size=9, bold=True, align="right")
    page.text(right - 8, y, "Amount", size=9, bold=True, align="right")
    for line in lines:
        y -= 24
        page.text(left + 8, y, line.description)
        page.text(370, y, str(line.quantity), align="right")
        page.text(455, y, s.number(line.unit), align="right")
        page.text(right - 8, y, s.number(line.amount), align="right")
        page.rule(left, y - 9, right, y - 9)

    y -= 40
    if variant == "cut_off":
        page.text(right, y, "Continued on page 2", size=9, grey=0.45, align="right")
    else:
        rows = [
            ("Subtotal", subtotal, False),
            (f"{supplier.tax_name} {supplier.tax_rate}%", tax, False),
        ]
        if variant != "no_total":
            rows.append(("Amount due", total, True))
        for label, amount, bold in rows:
            if bold:
                page.rule(370, y + 14, right, y + 14, grey=0.3)
            page.text(370, y, label, size=12 if bold else 10, bold=bold)
            page.text(
                right - 8,
                y,
                s.amount(amount, sym),
                size=12 if bold else 10,
                bold=bold,
                align="right",
            )
            y -= 20
    page.rule(left, 96, right, 96)
    page.text(left, 80, "Payment terms: 30 days. Thank you for your business.", size=9, grey=0.45)
    page.text(
        right,
        80,
        "Page 1 of 2" if variant == "cut_off" else "Page 1 of 1",
        size=9,
        grey=0.45,
        align="right",
    )

    cut = variant == "cut_off"
    answer = {
        "invoice_number": ref,
        "issued_on": issued.isoformat(),
        "due_date": due.isoformat(),
        "po_number": po,
        "vendor": supplier.name,
        "vendor_tax_id": supplier.tax_id,
        "bill_to": ", ".join(BILL_TO),
        "currency": supplier.currency,
        "subtotal": None if cut else str(subtotal),
        "discount": None,
        "tax_amount": None if cut else str(tax),
        "taxes": []
        if cut
        else [{"name": supplier.tax_name, "rate": f"{supplier.tax_rate}%", "amount": str(tax)}],
        # The model never adds anything up: an unprinted total is null.
        "total": None if cut or variant == "no_total" else str(total),
        "line_items": [
            {
                "description": line.description,
                "quantity": line.quantity,
                "unit_price": str(line.unit),
                "amount": str(line.amount),
            }
            for line in lines
        ],
    }
    shows = {
        "clean": "ready, and in the ledger",
        "no_total": "a total the invoice did not print, worked out by the processor's transform",
        "cut_off": "page 1 of 2 only: no total, so held for review",
        "credit": "a credit note: a negative total the invoice rules refuse",
    }[variant]
    slug = supplier.name.split()[0].lower()
    return Synthetic(ref, f"{slug}-{ref.lower()}.pdf", page.pdf(), answer, issued, shows)


def receipt(
    rng: random.Random, shop: Supplier, number: int, issued: date, *, variant: str = "clean"
) -> Synthetic:
    """
    A till receipt, as an emailed PDF. Variants: "clean"; "torn" (ends before
    the totals); "base_wrong" (base plus IVA printed does not make the total).
    """
    lines = _lines(rng, shop.items, 150, 4_500)
    total = sum((line.amount for line in lines), Decimal("0"))
    base = (total / (1 + shop.tax_rate / 100)).quantize(CENT, ROUND_HALF_UP)
    tax = total - base
    printed_base = base + Decimal("1.00") if variant == "base_wrong" else base
    ticket = f"Ticket {3300 + number}"
    s = shop.style
    hour = 8 + number % 11

    rows = 11 + len(lines) + (0 if variant == "torn" else 6)
    height = rows * 16 + 40
    page = Page(226, height, mono=True)
    centre, left, right = 113, 14, 212
    y = height - 30

    def write(text, x, align="left", size=8.5, bold=False):
        page.text(x, y, text, size=size, bold=bold, align=align)

    write(shop.name, centre, "center", 11, True)
    for part in shop.address:
        y -= 13
        write(part, centre, "center", 8)
    y -= 13
    write(f"CIF/NIF {shop.tax_id}", centre, "center", 8)
    y -= 20
    write(f"{s.day(issued)}  {hour:02d}:{(number * 17) % 60:02d}", left)
    write(ticket, right, "right")
    y -= 14
    write("-" * 36, centre, "center")
    for line in lines:
        y -= 14
        label = f"{line.quantity} x {line.description}" if line.quantity > 1 else line.description
        write(label[:26], left)
        write(s.number(line.amount), right, "right")
    if variant != "torn":
        y -= 14
        write("-" * 36, centre, "center")
        y -= 16
        write("TOTAL EUR", left, size=11, bold=True)
        write(s.number(total), right, "right", 11, True)
        y -= 14
        write(f"Base imponible {s.number(printed_base)}", left, size=8)
        y -= 12
        write(f"IVA {shop.tax_rate}% {s.number(tax)}", left, size=8)
        y -= 14
        write(shop.payment, left, size=8)
        y -= 18
        write("GRACIAS POR SU COMPRA", centre, "center", 8)

    torn = variant == "torn"
    answer = {
        "vendor": shop.name,
        "tax_id": shop.tax_id,
        "issued_on": issued.isoformat(),
        "subtotal": None if torn else str(printed_base),
        "tax_amount": None if torn else str(tax),
        "total": None if torn else str(total),
        "currency": "EUR",
        "payment_method": None if torn else shop.payment,
        "line_items": [
            {"name": line.description, "quantity": line.quantity, "amount": str(line.amount)}
            for line in lines
        ],
    }
    italian = shop.name == "COOP ITALIA"
    shows = {
        "clean": "an Italian tax ID the Spanish rule refuses: approve it as it is"
        if italian
        else "ready, and in the ledger",
        "torn": "torn before the totals: no total, so held for review",
        "base_wrong": "base plus IVA does not make the total, so held for review",
    }[variant]
    return Synthetic(ticket, f"ticket-{3300 + number}.pdf", page.pdf(), answer, issued, shows)


def delivery_note(rng: random.Random, number: int, issued: date) -> Synthetic:
    """A document no processor is set up for: a delivery note, with no prices."""
    page = Page()
    page.text(50, 772, "Globe Logistics BV", size=18, bold=True)
    page.text(545, 772, "DELIVERY NOTE", size=20, bold=True, align="right")
    page.text(380, 748, "Delivery no.", size=9, grey=0.45)
    page.text(545, 748, f"DN-{50200 + number}", size=9, align="right")
    page.text(380, 734, "Shipped", size=9, grey=0.45)
    page.text(545, 734, NL.day(issued), size=9, align="right")
    page.text(50, 690, "DELIVER TO", size=8, bold=True, grey=0.45)
    for i, line in enumerate(BILL_TO):
        page.text(50, 675 - i * 14, line, size=10)
    y = 600
    for item in rng.sample(INVOICERS[1].items, k=3):
        page.text(58, y, item)
        page.text(370, y, f"{rng.randint(1, 12)} pallets", align="right")
        y -= 24
    page.text(50, y - 30, "Goods received in good condition:  ____________________")
    ref = f"DN-{50200 + number}"
    return Synthetic(
        ref,
        f"globe-{ref.lower()}.pdf",
        page.pdf(),
        None,
        issued,
        "a document no processor recognises",
    )


def catalogue(today: date, *, seed: int = 7) -> list[Synthetic]:
    """The demo's documents: about thirty, over the last month, newest first."""
    rng = random.Random(seed)
    made: list[Synthetic] = []
    plan = [
        ("invoice", 0, "clean"), ("receipt", 0, "clean"), ("invoice", 1, "no_total"),
        ("receipt", 1, "clean"), ("invoice", 2, "cut_off"), ("receipt", 3, "clean"),
        ("invoice", 3, "clean"), ("receipt", 2, "clean"), ("invoice", 1, "clean"),
        ("receipt", 0, "base_wrong"), ("delivery", 0, ""), ("invoice", 0, "clean"),
        ("receipt", 1, "torn"), ("invoice", 2, "clean"), ("receipt", 2, "clean"),
        ("invoice", 3, "credit"), ("receipt", 0, "clean"), ("invoice", 1, "clean"),
        ("receipt", 3, "clean"), ("invoice", 0, "no_total"), ("receipt", 1, "clean"),
        ("invoice", 2, "clean"), ("receipt", 2, "clean"), ("invoice", 3, "clean"),
        ("invoice", 0, "clean"), ("receipt", 0, "clean"), ("invoice", 1, "clean"),
        ("receipt", 1, "clean"),
    ]  # fmt: skip
    for number, (kind, who, variant) in enumerate(plan, start=1):
        issued = today - timedelta(days=number + rng.randint(0, 2))
        if kind == "invoice":
            made.append(invoice(rng, INVOICERS[who], number, issued, variant=variant))
        elif kind == "receipt":
            made.append(receipt(rng, SHOPS[who], number, issued, variant=variant))
        else:
            made.append(delivery_note(rng, number, issued))
    return made
