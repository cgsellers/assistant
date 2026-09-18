"""OCR engines: the protocol, TSV parsing, and HTTP error mapping."""

from __future__ import annotations

import shutil

import httpx
import pytest

from ocr import get_engine
from ocr.base import OcrEngine, OcrError, OcrOutput, TextBlock, UnsupportedMediaType
from ocr.ppocr import PpOcrEngine, _to_output
from ocr.tesseract import TesseractEngine, _parse_tsv

HAS_TESSERACT = shutil.which("tesseract") is not None

# Real tesseract TSV. Built by joining rather than written as one literal so
# the column names stay readable: levels 1-4 are layout containers with
# conf -1 and no text, level 5 is a word.
_TSV_COLUMNS = (
    "level", "page_num", "block_num", "par_num", "line_num", "word_num",
    "left", "top", "width", "height", "conf", "text",
)
_TSV_ROWS = (
    (1, 1, 0, 0, 0, 0, 0, 0, 420, 360, -1, ""),        # page container
    (5, 1, 1, 1, 1, 1, 21, 23, 38, 10, 94.5, "CAFE"),
    (5, 1, 1, 1, 1, 2, 71, 23, 39, 10, 93.8, "LUNA"),
    (5, 1, 1, 1, 2, 1, 21, 47, 48, 11, -1, ""),        # word with no text
    (5, 1, 1, 1, 3, 1, 10, 90, 30, 12, 88.0, "9.35"),
)
SAMPLE_TSV = "\n".join(
    ["\t".join(_TSV_COLUMNS)] + ["\t".join(str(v) for v in row) for row in _TSV_ROWS]
) + "\n"


class TestProtocolConformance:
    @pytest.mark.parametrize("engine_class", [TesseractEngine, PpOcrEngine])
    def test_engines_satisfy_the_protocol(self, engine_class):
        """runtime_checkable Protocol -- swappability checked, not assumed."""
        assert isinstance(engine_class(), OcrEngine)

    def test_engines_report_a_name(self):
        assert TesseractEngine().name == "tesseract"
        assert PpOcrEngine().name == "ppocr-onnx"


class TestGetEngine:
    def test_returns_ppocr_by_default(self, settings):
        assert get_engine().name == "ppocr-onnx"

    def test_can_be_overridden_by_name(self, settings):
        assert get_engine("tesseract").name == "tesseract"
        assert get_engine("PPOCR").name == "ppocr-onnx"  # case-insensitive

    def test_rejects_an_unknown_engine(self, settings):
        with pytest.raises(ValueError, match="unknown ocr engine"):
            get_engine("magic-ocr")

    def test_honours_the_setting(self, settings, monkeypatch):
        from core.config import get_settings

        monkeypatch.setenv("OCR_ENGINE", "tesseract")
        get_settings.cache_clear()
        assert get_engine().name == "tesseract"


class TestParseTsv:
    def test_keeps_only_word_rows(self):
        """Levels 1-4 are layout containers with no text."""
        blocks = _parse_tsv(SAMPLE_TSV)
        assert [b.text for b in blocks] == ["CAFE", "LUNA", "9.35"]

    def test_converts_width_height_to_absolute_corners(self):
        block = _parse_tsv(SAMPLE_TSV)[0]
        assert block.bbox == (21, 23, 21 + 38, 23 + 10)

    def test_normalises_confidence_to_zero_to_one(self):
        """Every engine must report on one scale; tesseract uses 0-100."""
        assert _parse_tsv(SAMPLE_TSV)[0].confidence == pytest.approx(0.945)

    def test_drops_rows_with_no_text(self):
        assert all(b.text for b in _parse_tsv(SAMPLE_TSV))

    def test_records_the_page_number(self):
        assert all(b.page == 1 for b in _parse_tsv(SAMPLE_TSV))

    def test_tesseract_reports_no_polygon(self):
        assert _parse_tsv(SAMPLE_TSV)[0].polygon is None

    def test_empty_input_yields_no_blocks(self):
        assert _parse_tsv("") == []


class TestTesseractEngine:
    def test_refuses_pdf_permanently(self):
        """No rasteriser, and it should not guess -- retrying cannot help."""
        with pytest.raises(UnsupportedMediaType, match="cannot read application/pdf"):
            TesseractEngine().read(b"%PDF-1.7", "application/pdf")

    def test_refuses_an_unknown_type(self):
        with pytest.raises(UnsupportedMediaType):
            TesseractEngine().read(b"data", "application/zip")

    @pytest.mark.needs_tesseract
    @pytest.mark.skipif(not HAS_TESSERACT, reason="tesseract not on PATH")
    def test_reads_a_real_image(self, receipt_png):
        output = TesseractEngine().read(receipt_png, "image/png")
        assert output.engine == "tesseract"
        assert output.engine_version
        assert output.blocks
        assert 0.0 <= output.mean_confidence <= 1.0


class TestOcrOutput:
    def test_serialises_blocks_for_the_json_column(self):
        output = OcrOutput(
            full_text="x",
            blocks=[TextBlock(text="x", bbox=(1, 2, 3, 4), confidence=0.5, page=2)],
        )
        assert output.as_layout_blocks() == [
            {"text": "x", "bbox": [1, 2, 3, 4], "confidence": 0.5, "page": 2}
        ]

    def test_includes_a_polygon_only_when_present(self):
        with_polygon = OcrOutput(
            full_text="x",
            blocks=[
                TextBlock(
                    text="x", bbox=(1, 2, 3, 4), polygon=((1.0, 2.0), (3.0, 2.0),
                                                          (3.0, 4.0), (1.0, 4.0))
                )
            ],
        )
        assert with_polygon.as_layout_blocks()[0]["polygon"] == [
            [1.0, 2.0], [3.0, 2.0], [3.0, 4.0], [1.0, 4.0]
        ]

        without = OcrOutput(full_text="x", blocks=[TextBlock(text="x", bbox=(1, 2, 3, 4))])
        assert "polygon" not in without.as_layout_blocks()[0]


class TestPpOcrResponseMapping:
    def test_maps_a_service_response(self):
        output = _to_output(
            {
                "engine": "ppocr-onnx",
                "engine_version": "rapidocr 3.9.2",
                "params": {"dpi": 200, "source": "pdf"},
                "full_text": "TOTAL\n9.35",
                "mean_confidence": 0.98,
                "pages": 2,
                "elapsed_seconds": 1.5,
                "blocks": [
                    {
                        "text": "TOTAL",
                        "bbox": [1, 2, 3, 4],
                        "polygon": [[1, 2], [3, 2], [3, 4], [1, 4]],
                        "confidence": 0.99,
                        "page": 1,
                    }
                ],
            }
        )
        assert output.engine == "ppocr-onnx"
        assert output.full_text == "TOTAL\n9.35"
        assert output.mean_confidence == 0.98
        assert output.blocks[0].bbox == (1, 2, 3, 4)
        assert output.blocks[0].polygon == ((1, 2), (3, 2), (3, 4), (1, 4))
        # Reproducibility metadata for phase 3 evals.
        assert output.params["dpi"] == 200
        assert output.params["pages"] == 2

    def test_handles_a_blank_page(self):
        output = _to_output({"full_text": "", "blocks": [], "mean_confidence": None})
        assert output.blocks == []
        assert output.mean_confidence is None


class TestPpOcrErrorMapping:
    """
    The distinction the worker depends on: permanent failures must not retry,
    transient ones must.
    """

    def _engine_returning(self, monkeypatch, status_code, json_body=None):
        def fake_post(*args, **kwargs):
            return httpx.Response(
                status_code,
                json=json_body if json_body is not None else {"detail": "boom"},
                request=httpx.Request("POST", "http://ocr/ocr"),
            )

        monkeypatch.setattr(httpx, "post", fake_post)
        return PpOcrEngine(base_url="http://ocr")

    @pytest.mark.parametrize("status_code", [415, 413])
    def test_file_problems_are_permanent(self, monkeypatch, status_code):
        engine = self._engine_returning(monkeypatch, status_code)
        with pytest.raises(UnsupportedMediaType):
            engine.read(b"x", "application/pdf")

    @pytest.mark.parametrize("status_code", [500, 502, 503])
    def test_server_errors_are_transient(self, monkeypatch, status_code):
        engine = self._engine_returning(monkeypatch, status_code)
        with pytest.raises(OcrError):
            engine.read(b"x", "image/png")

    def test_an_unreachable_service_is_transient(self, monkeypatch):
        """Container restarting -- back off, do not condemn the document."""

        def refuse(*args, **kwargs):
            raise httpx.ConnectError("connection refused")

        monkeypatch.setattr(httpx, "post", refuse)
        with pytest.raises(OcrError, match="unreachable"):
            PpOcrEngine(base_url="http://ocr").read(b"x", "image/png")

    def test_a_timeout_is_transient(self, monkeypatch):
        def timeout(*args, **kwargs):
            raise httpx.ReadTimeout("too slow")

        monkeypatch.setattr(httpx, "post", timeout)
        with pytest.raises(OcrError, match="timed out"):
            PpOcrEngine(base_url="http://ocr").read(b"x", "image/png")

    def test_a_success_returns_output(self, monkeypatch):
        engine = self._engine_returning(
            monkeypatch,
            200,
            {"engine": "ppocr-onnx", "full_text": "hi", "blocks": [], "mean_confidence": None},
        )
        assert engine.read(b"x", "image/png").full_text == "hi"

    def test_healthy_is_false_when_unreachable(self, monkeypatch):
        def refuse(*args, **kwargs):
            raise httpx.ConnectError("nope")

        monkeypatch.setattr(httpx, "get", refuse)
        assert PpOcrEngine(base_url="http://ocr").healthy() is False
