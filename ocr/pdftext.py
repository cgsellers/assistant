"""
Reading a born-digital PDF's own text instead of OCRing a picture of it.

An invoice exported from accounting software already contains its text,
exactly, with every character's position. The OCR container nevertheless
rasterised every PDF at 200 DPI and recognised it back -- spending seconds
to replace exact text with a guess at it. On the Italian receipts, OCR
misreads were about a third of all field errors (`0.70` read as `8.70`);
for a PDF with a text layer that whole class of error does not need to
exist.

So for PDFs the text layer is read first, and OCR is the fallback for a
document that has none -- a scan saved as PDF. Fragments come from PDFium's
text rectangles, which are runs of text on one line: on a two-column invoice
the description and the amount arrive as separate fragments with exact
boxes, which is precisely the input row reconstruction needs.

Coordinates are converted from PDF space (points, origin bottom-left) to the
convention every other engine uses (origin top-left) and doubled, so integer
boxes keep half-point precision.
"""

from __future__ import annotations

from importlib.metadata import version

import pypdfium2 as pdfium

from ocr.base import OcrEngine, OcrOutput, TextBlock

#: Below this many non-space characters a PDF is treated as having no usable
#: text layer -- typically a scan, sometimes a page number and nothing else.
MIN_CHARS = 20

#: Points to box units. Two keeps half-point precision in integer boxes.
SCALE = 2


def read_text_layer(data: bytes) -> OcrOutput | None:
    """
    The PDF's embedded text with positions, or None if there is not enough
    of it to trust (a scan, an image-only export).
    """
    pdf = pdfium.PdfDocument(data)
    try:
        blocks: list[TextBlock] = []
        for number, page in enumerate(pdf, start=1):
            _, height = page.get_size()
            textpage = page.get_textpage()
            for i in range(textpage.count_rects()):
                left, bottom, right, top = textpage.get_rect(i)
                text = textpage.get_text_bounded(left, bottom, right, top).strip()
                if not text:
                    continue
                blocks.append(
                    TextBlock(
                        text=text,
                        bbox=(
                            round(left * SCALE),
                            round((height - top) * SCALE),
                            round(right * SCALE),
                            round((height - bottom) * SCALE),
                        ),
                        # Not a recognition score: nothing was recognised.
                        confidence=None,
                        page=number,
                    )
                )
    finally:
        pdf.close()

    if sum(len(b.text.replace(" ", "")) for b in blocks) < MIN_CHARS:
        return None

    return OcrOutput(
        full_text="\n".join(b.text for b in blocks),
        blocks=blocks,
        # Left as None rather than 1.0: the text is exact if the software that
        # wrote the PDF put it there, but a scanner's own OCR layer looks the
        # same from here and is not.
        mean_confidence=None,
        engine="pdf-text",
        engine_version=f"pypdfium2-{version('pypdfium2')}",
        params={"source": "embedded text layer", "scale": SCALE},
    )


class TextLayerFirst:
    """
    An OcrEngine that tries a PDF's text layer before the real engine.

    A wrapper rather than a branch in the worker, so the eval harness and
    the pipeline go through the same decision and cannot disagree about
    which documents were OCRed.
    """

    def __init__(self, fallback: OcrEngine):
        self.fallback = fallback
        self.name = f"pdf-text+{fallback.name}"

    def read(self, data: bytes, mime_type: str) -> OcrOutput:
        if mime_type == "application/pdf":
            try:
                output = read_text_layer(data)
            except pdfium.PdfiumError:
                # Malformed or encrypted: not this layer's problem to report.
                # The real engine gets the bytes and rejects them if it must.
                output = None
            if output is not None:
                return output
        return self.fallback.read(data, mime_type)
