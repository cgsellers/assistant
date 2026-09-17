"""OCR engines and the protocol they share."""

from __future__ import annotations

from ocr.base import OcrEngine

# Registry rather than an if/elif chain, so adding an engine is one line here
# and no change anywhere else.
_ENGINES = {"ppocr", "tesseract"}


def get_engine(name: str | None = None) -> OcrEngine:
    """
    Build the configured engine.

    `ppocr` is the default and runs the PP-OCR models in the `ocr` container.
    `tesseract` runs in-process and exists for tests and for working without
    Docker; it is materially less accurate -- see docs/DESIGN.md 5.12.
    """
    from core.config import get_settings

    name = (name or get_settings().ocr_engine).lower()

    if name == "ppocr":
        from ocr.ppocr import PpOcrEngine

        return PpOcrEngine()
    if name == "tesseract":
        from ocr.tesseract import TesseractEngine

        return TesseractEngine()

    raise ValueError(f"unknown ocr engine {name!r}; expected one of {sorted(_ENGINES)}")


__all__ = ["OcrEngine", "get_engine"]
