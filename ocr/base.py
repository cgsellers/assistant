"""
The OCR boundary.

Every engine -- Tesseract, PaddleOCR, a VLM -- implements `OcrEngine` and
returns the same shape. That is what lets phase 3 run several engines over one
labeled set and compare them, and what keeps the rest of the pipeline from
caring which one ran.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


class OcrError(Exception):
    """OCR failed for a reason worth recording on the document."""


class UnsupportedMediaType(OcrError):
    """This engine cannot read this format at all -- retrying will not help."""


@dataclass(frozen=True)
class TextBlock:
    """
    One recognised piece of text and where it sits on the page.

    Bounding boxes are not decoration: they are how the analyst agent cites a
    figure back to a spot on the original document, and how phase 2 tells a
    total apart from a line item that happens to have the same number.
    """

    text: str
    bbox: tuple[int, int, int, int]  # x0, y0, x1, y1 in pixels, origin top-left
    confidence: float | None = None  # 0.0-1.0; None for engines that don't report it
    page: int = 1
    # Detection outline, when the engine provides one. PP-OCR returns
    # quadrilaterals, which keep the rotation of skewed text that `bbox`
    # discards; Tesseract only reports rectangles and leaves this None.
    polygon: tuple[tuple[float, float], ...] | None = None


@dataclass(frozen=True)
class OcrOutput:
    """What every engine returns, and what gets written to `ocr_results`."""

    full_text: str
    blocks: list[TextBlock] = field(default_factory=list)
    mean_confidence: float | None = None
    engine: str = "unknown"
    engine_version: str | None = None
    # Whatever was needed to reproduce this run: language, dpi, model size.
    params: dict | None = None

    def as_layout_blocks(self) -> list[dict]:
        """JSON form for the `ocr_results.layout_blocks` column."""
        return [
            {
                "text": b.text,
                "bbox": list(b.bbox),
                "confidence": b.confidence,
                "page": b.page,
                **({"polygon": [list(p) for p in b.polygon]} if b.polygon else {}),
            }
            for b in self.blocks
        ]


@runtime_checkable
class OcrEngine(Protocol):
    """
    Implementations must be stateless and safe to call concurrently -- the
    worker may run several at once.
    """

    name: str

    def read(self, data: bytes, mime_type: str) -> OcrOutput:
        """
        Extract text from a document.

        Raises UnsupportedMediaType if the format is not readable by this
        engine (permanent -- do not retry), or OcrError for a failure that
        might succeed on a second attempt.
        """
        ...
