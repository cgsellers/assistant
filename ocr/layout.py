"""
Rebuilding printed lines from OCR fragments.

PP-OCR detects text as separate fragments, each with a bounding box, and its
reading order is not the printed order. On a two-column receipt -- product on
the left, price on the right -- the flattened text can list every name and
then every price, or the prices out of step with the names:

    DELIZIE SAC.G.GRAND#
    FERR KINDER LATTE X#
    EURO
    1,89          <- belongs to KINDER, the second line
    0,79          <- belongs to DELIZIE, the first

A model reading that pairs names with prices by position and gets them
wrong. On a real receipt it mis-paired nearly every item while still getting
the total right, because the total survives and the association does not.

The boxes say which price sits on which line, so this puts them back:

    DELIZIE SAC.G.GRAND#          0,79
    FERR KINDER LATTE X#          1,89

Fragments whose vertical centres fall within half a typical line height are
one printed line; each line is ordered left to right; and each fragment is
placed at a column proportional to its x position, so prices still read as a
column. No model is involved -- it is geometry, and it is tested.
"""

from __future__ import annotations

from statistics import median

from ocr.base import TextBlock

#: Fragments on one printed line rarely disagree by more than this fraction of
#: a line height. Generous enough for a slightly skewed photo; tight enough
#: that consecutive lines on a dense till roll stay apart.
SAME_LINE = 0.5

#: Minimum gap between two fragments on one line, so they never run together.
MIN_GAP = 2


def to_rows(blocks: list[TextBlock]) -> str:
    """The text of the page laid out as printed lines. Empty input, empty output."""
    if not blocks:
        return ""

    pages: dict[int, list[TextBlock]] = {}
    for block in blocks:
        if block.text.strip():
            pages.setdefault(block.page, []).append(block)

    return "\n\n".join(_page(pages[number]) for number in sorted(pages))


def _page(blocks: list[TextBlock]) -> str:
    heights = [b.bbox[3] - b.bbox[1] for b in blocks if b.bbox[3] > b.bbox[1]]
    line_height = median(heights) if heights else 1
    char_width = _char_width(blocks)
    slope = _slope(blocks)

    def level(block: TextBlock) -> float:
        # The height this fragment would sit at if the page were square. On a
        # receipt photographed at -3 degrees the right-hand column sits a full
        # line higher than its label; grouping on raw heights then pairs every
        # price with the line below it.
        cx = (block.bbox[0] + block.bbox[2]) / 2
        return _centre(block) - cx * slope

    rows: list[list[TextBlock]] = []
    centres: list[float] = []
    for block in sorted(blocks, key=level):
        here = level(block)
        # Compare against the row's running centre rather than its first
        # fragment, so a line does not split partway across.
        if rows and abs(here - centres[-1]) <= SAME_LINE * line_height:
            rows[-1].append(block)
            centres[-1] = sum(level(b) for b in rows[-1]) / len(rows[-1])
        else:
            rows.append([block])
            centres.append(here)

    return "\n".join(_render(row, char_width) for row in rows)


def _centre(block: TextBlock) -> float:
    return (block.bbox[1] + block.bbox[3]) / 2


#: Fragments narrower than this give an unreliable angle -- a two-character
#: price box tilts by whole degrees from a single pixel of jitter.
MIN_ANGLE_WIDTH = 15


def _slope(blocks: list[TextBlock]) -> float:
    """
    How far the page's text rises or falls per pixel across, from the
    detection outlines.

    PP-OCR returns each fragment as a quadrilateral that keeps its rotation,
    which `bbox` throws away. The median over all fragments is the page's
    tilt, robust to the odd crooked detection. Zero when there are no
    outlines (Tesseract reports rectangles only), which is the old behaviour.
    """
    slopes = []
    for block in blocks:
        outline = block.polygon
        if not outline or len(outline) < 2:
            continue
        (x0, y0), (x1, y1) = outline[0], outline[1]
        if x1 - x0 >= MIN_ANGLE_WIDTH:
            slopes.append((y1 - y0) / (x1 - x0))
    return median(slopes) if slopes else 0.0


def _char_width(blocks: list[TextBlock]) -> float:
    """Typical pixels per character, from the fragments themselves."""
    widths = [
        (b.bbox[2] - b.bbox[0]) / len(b.text.strip())
        for b in blocks
        if b.text.strip() and b.bbox[2] > b.bbox[0]
    ]
    return median(widths) if widths else 1.0


def _render(row: list[TextBlock], char_width: float) -> str:
    line = ""
    for block in sorted(row, key=lambda b: b.bbox[0]):
        column = round(block.bbox[0] / char_width)
        text = block.text.strip()
        if line:
            column = max(column, len(line) + MIN_GAP)
        line = line.ljust(column) + text
    return line.rstrip()


def blocks_from_json(layout_blocks: list[dict] | None) -> list[TextBlock]:
    """The stored `ocr_results.layout_blocks` column, back as TextBlocks."""
    out = []
    for item in layout_blocks or []:
        bbox = item.get("bbox")
        if not item.get("text") or not bbox or len(bbox) != 4:
            continue
        polygon = item.get("polygon")
        out.append(
            TextBlock(
                text=item["text"],
                bbox=tuple(int(v) for v in bbox),
                confidence=item.get("confidence"),
                page=item.get("page", 1),
                # Kept: it carries the rotation that row grouping corrects for.
                polygon=tuple(tuple(pt) for pt in polygon) if polygon else None,
            )
        )
    return out


def extraction_text(full_text: str, blocks: list[TextBlock]) -> str:
    """
    What the extractor is shown. One function, so the worker and the eval
    harness cannot render the same document two different ways.

    Rows rebuilt from the boxes when there are boxes; the engine's own text
    otherwise. Measured on a 19-item two-column receipt: flattened text put 1
    of 19 amounts on the right item, rebuilt rows put all 19, at the same cost.
    """
    rows = to_rows(blocks)
    return rows if rows.strip() else full_text
