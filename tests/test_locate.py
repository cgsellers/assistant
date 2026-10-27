"""
Finding where each value was printed (doctypes/locate.py): the highlights in
the client portal. Blocks here are shaped like each engine's: Tesseract's
single words, PP-OCR's whole lines.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from doctypes.definition import Definition
from doctypes.locate import amounts_in, dates_in, lines_of, locate

PAGE = [{"page": 1, "width": 1000, "height": 1000}]


def words(*rows: tuple[int, str]) -> list[dict]:
    """Tesseract-like blocks: one per word, each line at height y, 10px per character."""
    blocks = []
    for y, text in rows:
        x = 50
        for word in text.split():
            blocks.append({"text": word, "bbox": [x, y, x + 10 * len(word), y + 20], "page": 1})
            x += 10 * len(word) + 10
    return blocks


def invoice(**fields) -> Definition:
    spec = {
        "name": "invoice",
        "matches": {"keywords": ["invoice"]},
        "fields": {
            "invoice_number": {"type": "string", "description": "The INVOICE NO."},
            "issued_on": {"type": "date", "description": "The DATE of the invoice."},
            "subtotal": {"type": "money", "description": "Before tax, labelled SUBTOTAL."},
            "total": {"type": "money", "description": "The amount due, labelled TOTAL."},
            "quantity": {"type": "number", "description": "How many."},
            "line_items": {
                "type": "list",
                "description": "Each line.",
                "item_fields": {
                    "description": {"type": "string", "description": "What."},
                    "amount": {"type": "money", "description": "How much."},
                },
            },
            **fields,
        },
    }
    return Definition.model_validate(spec)


class TestReadingPrintedValues:
    @pytest.mark.parametrize(
        ("printed", "value"),
        [("91,19", "91.19"), ("1.234,56", "1234.56"), ("1,234.56", "1234.56"), ("1234", "1234")],
    )
    def test_amounts_in_any_local_style(self, printed, value):
        assert Decimal(value) in amounts_in(printed)

    def test_an_ambiguous_number_is_both_of_its_readings(self):
        assert amounts_in("1,234") == {Decimal("1234"), Decimal("1.234")}

    @pytest.mark.parametrize(
        "printed",
        [
            "24/10/2026",
            "24-10-2026",
            "10/24/2026",
            "24.10.26",
            "2026-10-24",
            "24 Oct 2026",
            "Oct 24, 2026",
            "24 ottobre 2026",
            "24 de oct. 2026",
            "24 de octubre de 2026",
        ],
    )
    def test_dates_day_first_month_first_and_in_words(self, printed):
        assert any(date(2026, 10, 24) in readings for _, _, readings in dates_in(f"Date {printed}"))


class TestLines:
    def test_words_side_by_side_are_one_line_in_reading_order(self):
        blocks = words((100, "Invoice no. INV-7701"))
        blocks.reverse()
        [line] = lines_of(blocks)
        assert line.text == "Invoice no. INV-7701"

    def test_lines_further_apart_than_half_a_line_are_separate(self):
        assert [
            line.text for line in lines_of(words((100, "Subtotal 75,36"), (130, "Total 91,19")))
        ] == [
            "Subtotal 75,36",
            "Total 91,19",
        ]


class TestLocate:
    def test_each_value_is_boxed_where_it_was_printed_as_fractions_of_the_page(self):
        blocks = words(
            (100, "INVOICE no. INV-7701"),
            (130, "Date 24-10-2026"),
            (600, "Subtotal 75,36"),
            (630, "TOTAL 91,19"),
        )
        found = locate(
            invoice(),
            {
                "invoice_number": "INV-7701",
                "issued_on": "2026-10-24",
                "subtotal": "75.36",
                "total": "91.19",
            },
            blocks,
            PAGE,
        )
        assert found["invoice_number"]["page"] == 1
        left, top, right, bottom = found["invoice_number"]["box"]
        # "INV-7701" starts after "INVOICE no. " at x=50+70+10+30+10 = 170.
        assert left == pytest.approx(0.170 - 0.004)
        assert top == pytest.approx(0.100 - 0.004)
        assert right == pytest.approx(0.250 + 0.004)
        assert bottom == pytest.approx(0.120 + 0.004)
        assert found["issued_on"]["box"][1] == pytest.approx(0.126)
        assert found["total"]["box"][1] == pytest.approx(0.626)

    def test_when_a_value_appears_twice_the_labelled_line_wins(self):
        # No tax: the subtotal and the total are the same figure.
        blocks = words((600, "SUBTOTAL 91,19"), (630, "TOTAL 91,19"), (900, "Paid 91,19"))
        found = locate(invoice(), {"subtotal": "91.19", "total": "91.19"}, blocks, PAGE)
        assert found["subtotal"]["box"][1] == pytest.approx(0.596)
        assert found["total"]["box"][1] == pytest.approx(0.626)

    def test_a_small_number_is_not_looked_for(self):
        found = locate(invoice(), {"quantity": 2}, words((100, "2 x Coffee 2")), PAGE)
        assert "quantity" not in found

    def test_rows_are_boxed_as_their_whole_printed_line(self):
        blocks = words(
            (300, "Fuel surcharge 3 4,43 13,29"), (330, "Customs clearance 3 11,82 35,46")
        )
        rows = [
            {"description": "Customs clearance", "amount": "35.46"},
            {"description": "Something unprinted", "amount": "13.29"},
        ]
        found = locate(invoice(), {"line_items": rows}, blocks, PAGE)
        assert found["line_items.0"]["box"][1] == pytest.approx(0.326)
        # Found by its amount instead, on the line no other row took.
        assert found["line_items.1"]["box"][1] == pytest.approx(0.296)

    def test_a_row_goes_to_the_line_carrying_most_of_it(self):
        # As on a real Northwind invoice: the tax row's name also heads the page.
        blocks = words((100, "VAT No GB987654321"), (600, "Subtotal 87,40"), (640, "VAT 20% 17,48"))
        spec = invoice(
            taxes={
                "type": "list",
                "description": "Each tax.",
                "item_fields": {
                    "name": {"type": "string", "description": "Its name."},
                    "amount": {"type": "money", "description": "How much."},
                },
            }
        )
        found = locate(spec, {"taxes": [{"name": "VAT", "amount": "17.48"}]}, blocks, PAGE)
        assert found["taxes.0"]["box"][1] == pytest.approx(0.636)

    def test_ppocr_lines_work_the_same(self):
        blocks = [{"text": "TOTAL EUR 41,38", "bbox": [100, 700, 600, 740], "page": 1}]
        found = locate(invoice(), {"total": "41.38"}, blocks, PAGE)
        assert found["total"]["box"] == pytest.approx([0.096, 0.696, 0.604, 0.744])

    def test_nothing_is_placed_without_the_page_size(self):
        assert locate(invoice(), {"total": "91.19"}, words((630, "TOTAL 91,19")), None) == {}
