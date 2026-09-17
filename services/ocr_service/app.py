"""
OCR sidecar.

Wraps the PP-OCR detection and recognition models (running on ONNX Runtime)
behind a small HTTP API. Lives in its own container for two reasons: it keeps a
heavy native dependency out of the main application, and it is the process that
parses untrusted uploaded files, so it gets no database credentials and no
outbound network access.

The main app talks to this through ocr/ppocr.py, which implements the
OcrEngine protocol. Nothing else in the pipeline knows this service exists.
"""

from __future__ import annotations

import io
import logging
import os
import time
from collections.abc import Iterator
from contextlib import asynccontextmanager

import pypdfium2 as pdfium
from fastapi import FastAPI, File, HTTPException, Query, UploadFile, status
from rapidocr import RapidOCR

log = logging.getLogger("ocr_service")

# Rasterisation resolution for PDFs. 200 DPI is the usual sweet spot for
# receipts: high enough for small print, low enough that a multi-page invoice
# does not blow up memory.
DEFAULT_DPI = int(os.getenv("OCR_DPI", "200"))

# A 500-page PDF would tie this service up for minutes. Refuse rather than
# quietly take forever.
MAX_PAGES = int(os.getenv("OCR_MAX_PAGES", "20"))

IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp", "image/tiff", "image/heic"}
PDF_TYPE = "application/pdf"

# Loaded once at startup, not per request -- model initialisation is the
# expensive part and the engine is reusable.
_engine: RapidOCR | None = None
_versions: dict[str, str] = {}


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global _engine
    from importlib.metadata import version

    started = time.perf_counter()
    _engine = RapidOCR()
    # importlib.metadata reads the installed distribution rather than a
    # __version__ attribute, which rapidocr does not define.
    _versions.update(
        rapidocr=version("rapidocr"),
        onnxruntime=version("onnxruntime"),
    )
    log.info("engine ready in %.2fs (%s)", time.perf_counter() - started, _versions)
    yield
    _engine = None


app = FastAPI(title="Ledgerline OCR", version="0.1.0", lifespan=lifespan)


def _polygon_to_bbox(polygon) -> list[int]:
    """
    Collapse a 4-point detection polygon to an axis-aligned box.

    PP-OCR returns quadrilaterals, which carry rotation information that a
    plain rectangle loses. The polygon is preserved separately in the response
    so a future caller can use it; bbox is the simple form every engine can
    produce.
    """
    xs = [float(p[0]) for p in polygon]
    ys = [float(p[1]) for p in polygon]
    return [int(min(xs)), int(min(ys)), int(max(xs)), int(max(ys))]


def _read_one(image_bytes: bytes, page: int) -> list[dict]:
    """Run the models over a single rasterised page."""
    result = _engine(image_bytes)

    # A blank page yields no detections at all, not an empty list.
    if result.boxes is None or result.txts is None:
        return []

    blocks = []
    for polygon, text, score in zip(result.boxes, result.txts, result.scores, strict=False):
        text = (text or "").strip()
        if not text:
            continue
        blocks.append(
            {
                "text": text,
                "bbox": _polygon_to_bbox(polygon),
                "polygon": [[float(p[0]), float(p[1])] for p in polygon],
                "confidence": float(score),
                "page": page,
            }
        )
    return blocks


def _pdf_to_pages(data: bytes, dpi: int) -> Iterator[bytes]:
    """Rasterise each PDF page to PNG bytes."""
    document = pdfium.PdfDocument(data)
    try:
        if len(document) > MAX_PAGES:
            raise HTTPException(
                status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                f"PDF has {len(document)} pages, limit is {MAX_PAGES}",
            )
        for page in document:
            # PDF user units are 1/72 inch, so scale is dpi/72.
            bitmap = page.render(scale=dpi / 72)
            buffer = io.BytesIO()
            bitmap.to_pil().save(buffer, format="PNG")
            yield buffer.getvalue()
    finally:
        document.close()


@app.get("/health")
def health() -> dict:
    """
    Readiness, not just liveness: reports whether the models are actually
    loaded, because an unloaded engine cannot serve anything.
    """
    if _engine is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "engine not loaded")
    return {"status": "ok", "engine": "ppocr-onnx", "versions": _versions}


@app.post("/ocr")
async def ocr(
    file: UploadFile = File(description="Image or PDF"),
    dpi: int = Query(default=DEFAULT_DPI, ge=72, le=600, description="PDF rasterisation DPI"),
) -> dict:
    """
    Extract text, bounding boxes and per-word confidences.

    Accepts images directly and rasterises PDFs page by page. Returns the same
    shape regardless, with `page` distinguishing them.
    """
    if _engine is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "engine not loaded")

    data = await file.read()
    if not data:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "empty file")

    # The caller (core/storage.sniff_mime) has already identified the bytes, so
    # this is a routing decision rather than a trust decision.
    content_type = (file.content_type or "").split(";")[0].strip()

    started = time.perf_counter()
    if content_type == PDF_TYPE:
        blocks = [
            block
            for page_number, png in enumerate(_pdf_to_pages(data, dpi), start=1)
            for block in _read_one(png, page_number)
        ]
        pages = max((b["page"] for b in blocks), default=0)
        params = {"dpi": dpi, "source": "pdf"}
    elif content_type in IMAGE_TYPES or not content_type:
        blocks = _read_one(data, 1)
        pages = 1
        params = {"source": "image"}
    else:
        raise HTTPException(
            status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, f"cannot read {content_type!r}"
        )

    confidences = [b["confidence"] for b in blocks]

    return {
        "engine": "ppocr-onnx",
        "engine_version": f"rapidocr {_versions.get('rapidocr')}"
        f" / onnxruntime {_versions.get('onnxruntime')}",
        "params": {**params, **_versions},
        # Newline-joined: PP-OCR returns one entry per detected region, so a
        # newline preserves the visual line structure better than a space.
        "full_text": "\n".join(b["text"] for b in blocks),
        "blocks": blocks,
        "mean_confidence": sum(confidences) / len(confidences) if confidences else None,
        "pages": pages,
        "elapsed_seconds": round(time.perf_counter() - started, 3),
    }
