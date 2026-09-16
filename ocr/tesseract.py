"""
Tesseract engine -- the test double.

Chosen for tests and local development because it needs no GPU, no container
and no network: `brew install tesseract` and it runs. Accuracy on photographed
receipts is noticeably worse than PaddleOCR, which is fine. Its job is to prove
the pipeline works, not to be the engine that ships.

Shells out to the CLI rather than binding libtesseract: one less build
dependency, and the subprocess is a crude sandbox for untrusted image data.
"""

from __future__ import annotations

import csv
import io
import re
import subprocess
import tempfile
from pathlib import Path

from ocr.base import OcrError, OcrOutput, TextBlock, UnsupportedMediaType

# Tesseract reads images. PDFs need rasterising first, which is a job for the
# PaddleOCR container -- this engine declares the gap rather than guessing.
_READABLE = {"image/jpeg", "image/png", "image/tiff", "image/webp"}

_TIMEOUT_SECONDS = 120


class TesseractEngine:
    name = "tesseract"

    def __init__(self, lang: str = "eng", timeout: int = _TIMEOUT_SECONDS) -> None:
        self.lang = lang
        self.timeout = timeout

    def read(self, data: bytes, mime_type: str) -> OcrOutput:
        if mime_type not in _READABLE:
            raise UnsupportedMediaType(
                f"tesseract cannot read {mime_type}; it handles {sorted(_READABLE)}"
            )

        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "input"
            source.write_bytes(data)
            tsv = self._run(source)

        blocks = _parse_tsv(tsv)
        confidences = [b.confidence for b in blocks if b.confidence is not None]

        return OcrOutput(
            # Reconstructed from recognised words rather than a second
            # tesseract call, so text and blocks can never disagree.
            full_text=" ".join(b.text for b in blocks),
            blocks=blocks,
            mean_confidence=sum(confidences) / len(confidences) if confidences else None,
            engine=self.name,
            engine_version=self.version(),
            params={"lang": self.lang, "psm": "default", "output": "tsv"},
        )

    def _run(self, source: Path) -> str:
        try:
            result = subprocess.run(
                ["tesseract", str(source), "stdout", "-l", self.lang, "tsv"],
                capture_output=True,
                timeout=self.timeout,
                check=False,
            )
        except FileNotFoundError as exc:
            raise OcrError("tesseract is not installed or not on PATH") from exc
        except subprocess.TimeoutExpired as exc:
            raise OcrError(f"tesseract timed out after {self.timeout}s") from exc

        if result.returncode != 0:
            detail = result.stderr.decode("utf-8", "replace").strip()
            # A file tesseract cannot decode is a permanent failure: the bytes
            # will not improve on a retry.
            if "Error in pixRead" in detail or "Image file" in detail:
                raise UnsupportedMediaType(f"tesseract could not decode the image: {detail}")
            raise OcrError(f"tesseract exited {result.returncode}: {detail}")

        return result.stdout.decode("utf-8", "replace")

    def version(self) -> str | None:
        try:
            out = subprocess.run(
                ["tesseract", "--version"], capture_output=True, timeout=10, check=False
            ).stdout.decode("utf-8", "replace")
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return None
        match = re.search(r"tesseract\s+([\w.\-]+)", out)
        return match.group(1) if match else None


def _parse_tsv(tsv: str) -> list[TextBlock]:
    """
    Turn tesseract's TSV into blocks.

    The TSV has a row per layout element at every level; level 5 is a word.
    Rows above that are containers with conf = -1 and no text, so they are
    dropped. Confidence is reported 0-100 and normalised to 0.0-1.0 here so
    every engine reports on the same scale.
    """
    blocks: list[TextBlock] = []

    for row in csv.DictReader(io.StringIO(tsv), delimiter="\t", quoting=csv.QUOTE_NONE):
        if row.get("level") != "5":
            continue
        text = (row.get("text") or "").strip()
        if not text:
            continue

        try:
            left, top = int(row["left"]), int(row["top"])
            width, height = int(row["width"]), int(row["height"])
            conf = float(row["conf"])
        except (KeyError, ValueError):
            continue

        blocks.append(
            TextBlock(
                text=text,
                bbox=(left, top, left + width, top + height),
                confidence=conf / 100.0 if conf >= 0 else None,
                page=int(row.get("page_num") or 1),
            )
        )

    return blocks
