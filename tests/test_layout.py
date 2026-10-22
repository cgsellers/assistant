"""
Rebuilding printed lines from OCR fragments.

On a real two-column receipt, flattened OCR text put 1 of 19 prices on the
right product; rebuilt rows put all 19. These pin the geometry that made the
difference.
"""

from __future__ import annotations

from ocr.base import TextBlock
from ocr.layout import blocks_from_json, extraction_text, to_rows


def block(text, x0, y0, x1=None, y1=None, page=1):
    x1 = x1 if x1 is not None else x0 + 10 * len(text)
    y1 = y1 if y1 is not None else y0 + 20
    return TextBlock(text=text, bbox=(x0, y0, x1, y1), page=page)


class TestPairing:
    def test_a_name_and_its_price_become_one_line(self):
        rows = to_rows([block("DELIZIE", 10, 100), block("0,79", 300, 101)])
        assert rows.split() == ["DELIZIE", "0,79"]
        assert len(rows.splitlines()) == 1

    def test_the_case_that_broke_extraction(self):
        """
        OCR's reading order listed both names, then the prices out of step.
        The boxes put each price back on its own line.
        """
        fragments = [
            block("DELIZIE", 10, 100),
            block("KINDER", 10, 130),
            block("1,89", 300, 131),  # emitted first, but it is KINDER's
            block("0,79", 300, 100),
        ]
        lines = [line.split() for line in to_rows(fragments).splitlines()]
        assert lines == [["DELIZIE", "0,79"], ["KINDER", "1,89"]]

    def test_fragments_on_a_line_are_ordered_left_to_right(self):
        rows = to_rows([block("9.35", 300, 50), block("TOTAL", 10, 50)])
        assert rows.split() == ["TOTAL", "9.35"]

    def test_consecutive_lines_on_a_dense_roll_stay_apart(self):
        rows = to_rows([block("A", 10, 100), block("B", 10, 122)])
        assert [line.strip() for line in rows.splitlines()] == ["A", "B"]

    def test_a_slightly_skewed_line_is_not_split(self):
        """A phone photo is rarely square: the right end sits a little lower."""
        rows = to_rows([block("ROBERTI", 10, 100), block("2,83", 300, 108)])
        assert len(rows.splitlines()) == 1


class TestLayout:
    def test_prices_line_up_as_a_column(self):
        rows = to_rows(
            [
                block("SHORT", 10, 100),
                block("1,00", 300, 100),
                block("A MUCH LONGER NAME", 10, 130),
                block("2,00", 300, 130),
            ]
        )
        first, second = rows.splitlines()
        assert first.index("1,00") == second.index("2,00")

    def test_fragments_never_run_together(self):
        # DEF's box starts inside ABC's: the column maths alone would overlap them.
        rows = to_rows([block("ABC", 10, 100, x1=200), block("DEF", 20, 100)])
        assert "ABCDEF" not in rows
        assert rows.index("DEF") - rows.index("ABC") >= len("ABC") + 2

    def test_pages_are_kept_separate(self):
        rows = to_rows([block("PAGE ONE", 10, 100, page=1), block("PAGE TWO", 10, 100, page=2)])
        assert [page.strip() for page in rows.split("\n\n")] == ["PAGE ONE", "PAGE TWO"]

    def test_empty_fragments_are_dropped(self):
        assert to_rows([block("  ", 10, 100), block("X", 10, 140)]).strip() == "X"

    def test_nothing_in_nothing_out(self):
        assert to_rows([]) == ""


class TestStoredBlocks:
    def test_the_stored_json_round_trips(self):
        stored = [{"text": "TOTAL", "bbox": [10, 50, 60, 70], "confidence": 0.99, "page": 1}]
        assert blocks_from_json(stored)[0].bbox == (10, 50, 60, 70)

    def test_malformed_entries_are_skipped(self):
        stored = [{"text": "X"}, {"bbox": [1, 2, 3, 4]}, {"text": "Y", "bbox": [1, 2, 3]}]
        assert blocks_from_json(stored) == []

    def test_a_null_column_is_no_blocks(self):
        assert blocks_from_json(None) == []


class TestWhatTheExtractorSees:
    def test_rows_when_there_are_boxes(self):
        fragments = [block("TOTAL", 10, 50), block("9.35", 300, 50)]
        assert extraction_text("TOTAL\n9.35", fragments).split() == ["TOTAL", "9.35"]
        assert len(extraction_text("TOTAL\n9.35", fragments).splitlines()) == 1

    def test_the_engine_text_when_there_are_none(self):
        """An engine that reports no boxes still gets extracted."""
        assert extraction_text("TOTAL 9.35", []) == "TOTAL 9.35"


def tilted(text, x0, y0, slope, width=None, height=20):
    """A fragment on a page rotated so text rises `slope` px per px across."""
    width = width or 10 * len(text)
    lift = round(width * slope)
    polygon = (
        (x0, y0),
        (x0 + width, y0 + lift),
        (x0 + width, y0 + lift + height),
        (x0, y0 + height),
    )
    ys = [p[1] for p in polygon]
    return TextBlock(text=text, bbox=(x0, min(ys), x0 + width, max(ys)), polygon=polygon)


class TestTilt:
    """
    A receipt photographed at -3.3 degrees lifted the right-hand column by a
    full line, and grouping on raw heights paired every price with the line
    below it. The detection outlines carry the angle; these pin the
    correction.
    """

    SLOPE = -0.058  # about -3.3 degrees: rising to the right

    def _receipt(self, slope):
        # Each price sits a line higher than its label once the tilt is applied.
        lines = [("BIS BORSE", "3,90"), ("GHIACCIO", "0,99"), ("TOTALE", "5,88")]
        out = []
        for i, (name, price) in enumerate(lines):
            y = 100 + 22 * i
            out.append(tilted(name, 10, y, slope, width=120))
            out.append(tilted(price, 300, y + round(300 * slope), slope, width=40))
        return out

    def test_a_tilted_receipt_pairs_each_price_with_its_own_line(self):
        lines = [line.split() for line in to_rows(self._receipt(self.SLOPE)).splitlines()]
        assert lines == [["BIS", "BORSE", "3,90"], ["GHIACCIO", "0,99"], ["TOTALE", "5,88"]]

    def test_without_outlines_the_same_page_mis_pairs(self):
        """The failure the correction exists for, pinned so it cannot quietly return."""
        flat = [
            TextBlock(text=b.text, bbox=b.bbox) for b in self._receipt(self.SLOPE)
        ]
        lines = [line.split() for line in to_rows(flat).splitlines()]
        assert ["BIS", "BORSE", "3,90"] not in lines

    def test_a_level_page_is_unchanged(self):
        lines = [line.split() for line in to_rows(self._receipt(0.0)).splitlines()]
        assert lines == [["BIS", "BORSE", "3,90"], ["GHIACCIO", "0,99"], ["TOTALE", "5,88"]]

    def test_narrow_fragments_do_not_set_the_angle(self):
        """A two-character box tilts by whole degrees from one pixel of jitter."""
        from ocr.layout import _slope

        jitter = TextBlock(text="9", bbox=(0, 0, 8, 20), polygon=((0, 0), (8, 6), (8, 26), (0, 20)))
        level = tilted("A LONG LEVEL LINE", 10, 100, 0.0, width=200)
        assert _slope([jitter, level]) == 0.0

    def test_stored_outlines_survive_the_round_trip(self):
        """The worker reads blocks back from JSON; losing polygons loses the angle."""
        outline = [[0, 0], [40, -2], [40, 18], [0, 20]]
        stored = [{"text": "X", "bbox": [0, 0, 40, 20], "polygon": outline}]
        assert blocks_from_json(stored)[0].polygon[1] == (40, -2)
