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
    from ocr.pdftext import TextLayerFirst

    name = (name or get_settings().ocr_engine).lower()

    # PP-OCR is not wrapped: the container reads a PDF's text layer itself,
    # so untrusted PDFs are parsed only there, where there are no credentials
    # (DESIGN §8.9). Tesseract runs in this process and parses its images
    # here anyway -- it is the test double, never the production path -- so
    # it reads text layers in-process too.
    if name == "ppocr":
        from ocr.ppocr import PpOcrEngine

        return PpOcrEngine()
    if name == "tesseract":
        from ocr.tesseract import TesseractEngine

        return TextLayerFirst(TesseractEngine())

    raise ValueError(f"unknown ocr engine {name!r}; expected one of {sorted(_ENGINES)}")


__all__ = ["OcrEngine", "get_engine"]
