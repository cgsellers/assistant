"""
PP-OCR engine client.

The models run in the `ocr` container (see services/ocr_service/), not in this
process. This class is a thin HTTP client that implements OcrEngine, so the
worker calls it exactly as it calls Tesseract and never learns the difference.

Why the split: ONNX Runtime and the models are a heavy native dependency, and
this is the code path that parses files uploaded by strangers. Keeping it in a
separate container means the parser holds no database credentials.
"""

from __future__ import annotations

import httpx

from core.config import get_settings
from ocr.base import OcrError, OcrOutput, TextBlock, UnsupportedMediaType

# Generous: a 20-page PDF at 200 DPI took ~8s per page in testing, and the
# worker is not latency-sensitive.
DEFAULT_TIMEOUT_SECONDS = 300


class PpOcrEngine:
    name = "ppocr-onnx"

    def __init__(self, base_url: str | None = None, timeout: float | None = None) -> None:
        self.base_url = (base_url or get_settings().ocr_service_url).rstrip("/")
        self.timeout = timeout or DEFAULT_TIMEOUT_SECONDS

    def read(self, data: bytes, mime_type: str) -> OcrOutput:
        try:
            response = httpx.post(
                f"{self.base_url}/ocr",
                files={"file": ("document", data, mime_type)},
                timeout=self.timeout,
            )
        except httpx.TimeoutException as exc:
            raise OcrError(f"ocr service timed out after {self.timeout}s") from exc
        except httpx.RequestError as exc:
            # Container down or restarting. Transient by nature -- the worker
            # should back off and try again rather than fail the document.
            raise OcrError(f"ocr service unreachable at {self.base_url}: {exc}") from exc

        # 415 (wrong format) and 413 (too many pages) are properties of the
        # file, not of the service, so no retry will change the outcome.
        if response.status_code in (415, 413):
            raise UnsupportedMediaType(_detail(response))
        if response.status_code >= 500:
            raise OcrError(f"ocr service returned {response.status_code}: {_detail(response)}")
        if response.status_code != 200:
            raise OcrError(f"ocr service rejected the request: {_detail(response)}")

        return _to_output(response.json())

    def healthy(self) -> bool:
        """Whether the container is up with its models loaded."""
        try:
            return httpx.get(f"{self.base_url}/health", timeout=10).status_code == 200
        except httpx.RequestError:
            return False


def _detail(response: httpx.Response) -> str:
    try:
        return str(response.json().get("detail", response.text))[:500]
    except ValueError:
        return response.text[:500]


def _to_output(payload: dict) -> OcrOutput:
    blocks = [
        TextBlock(
            text=b["text"],
            bbox=tuple(b["bbox"]),
            confidence=b.get("confidence"),
            page=b.get("page", 1),
            polygon=tuple(tuple(p) for p in b["polygon"]) if b.get("polygon") else None,
        )
        for b in payload.get("blocks", [])
    ]
    return OcrOutput(
        full_text=payload.get("full_text", ""),
        blocks=blocks,
        mean_confidence=payload.get("mean_confidence"),
        engine=payload.get("engine", "ppocr-onnx"),
        engine_version=payload.get("engine_version"),
        # Carries dpi, page source and library versions, so an eval run in
        # phase 3 can be reproduced from the stored row.
        params={
            **payload.get("params", {}),
            "pages": payload.get("pages"),
            "elapsed_seconds": payload.get("elapsed_seconds"),
        },
    )
