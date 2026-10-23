"""
Born-digital PDFs read from their own text layer.

The claim is that a PDF which already contains its text never reaches OCR,
so OCR misreads cannot happen to it -- and that a scan saved as PDF still
does, because it has nothing else to offer.
"""

from __future__ import annotations

import io

import pytest
from PIL import Image

from ocr.base import OcrError
from ocr.layout import to_rows
from ocr.pdftext import MIN_CHARS, TextLayerFirst, read_text_layer

from .conftest import FakeEngine
from .pdfs import INVOICE, make_pdf


class NeverCalled:
    """Fails the test if OCR runs -- the point of reading the text layer."""

    name = "never"

    def read(self, data, mime_type):
        raise AssertionError("OCR was called on a PDF that has a text layer")


def scanned_pdf() -> bytes:
    """A PDF that is only a picture, as a scanner would produce."""
    buf = io.BytesIO()
    Image.new("RGB", (400, 600), "white").save(buf, format="PDF")
    return buf.getvalue()


class TestReadingTheTextLayer:
    def test_the_text_comes_back_exactly(self):
        out = read_text_layer(make_pdf(INVOICE))
        texts = [b.text for b in out.blocks]
        assert "ACME SUPPLIES LTD" in texts
        assert "143.39" in texts

    def test_it_says_where_it_came_from(self):
        out = read_text_layer(make_pdf(INVOICE))
        assert out.engine == "pdf-text"
        assert out.engine_version.startswith("pypdfium2-")

    def test_confidence_is_not_claimed(self):
        """Nothing was recognised, and a scanner's OCR layer looks identical from here."""
        assert read_text_layer(make_pdf(INVOICE)).mean_confidence is None

    def test_boxes_use_the_top_left_origin_like_every_other_engine(self):
        out = read_text_layer(make_pdf(INVOICE))
        top = next(b for b in out.blocks if b.text == "ACME SUPPLIES LTD")
        bottom = next(b for b in out.blocks if b.text == "143.39")
        assert top.bbox[1] < bottom.bbox[1]

    def test_columns_arrive_as_separate_fragments(self):
        """What row reconstruction needs: description and amount apart, each boxed."""
        texts = [b.text for b in read_text_layer(make_pdf(INVOICE)).blocks]
        assert "Toner cartridge" in texts
        assert "89.99" in texts

    def test_pages_are_numbered(self):
        two = make_pdf([[(50, 700, "First page heading text")], [(50, 700, "Second page text")]])
        assert {b.page for b in read_text_layer(two).blocks} == {1, 2}

    def test_a_scan_has_no_text_layer(self):
        assert read_text_layer(scanned_pdf()) is None

    def test_a_near_empty_layer_is_not_trusted(self):
        """A page number alone is not a document's text."""
        assert read_text_layer(make_pdf([[(50, 50, "1")]])) is None
        assert MIN_CHARS > 1


class TestRowsFromATextLayer:
    def test_a_two_column_invoice_pairs_every_amount_with_its_line(self):
        rows = to_rows(read_text_layer(make_pdf(INVOICE)).blocks)
        lines = [line.split() for line in rows.splitlines()]
        assert ["Toner", "cartridge", "89.99"] in lines
        assert ["Delivery", "5.00"] in lines
        assert ["Total", "due", "143.39"] in lines


class TestTheWrapper:
    def test_a_digital_pdf_never_reaches_ocr(self):
        engine = TextLayerFirst(NeverCalled())
        assert engine.read(make_pdf(INVOICE), "application/pdf").engine == "pdf-text"

    def test_a_scanned_pdf_falls_back_to_ocr(self):
        fallback = FakeEngine()
        TextLayerFirst(fallback).read(scanned_pdf(), "application/pdf")
        assert fallback.calls

    def test_images_go_straight_to_ocr(self):
        fallback = FakeEngine()
        TextLayerFirst(fallback).read(b"\x89PNG...", "image/png")
        assert fallback.calls

    def test_a_corrupt_pdf_is_handed_to_the_engine_not_crashed_on(self):
        fallback = FakeEngine(raises=OcrError("cannot read"))
        with pytest.raises(OcrError, match="cannot read"):
            TextLayerFirst(fallback).read(b"%PDF-1.4 garbage", "application/pdf")

    def test_a_pdf_over_the_page_limit_is_left_to_the_container_to_refuse(self):
        """Not walked in the worker: the container's limit applies to it too."""
        from ocr.pdftext import MAX_PAGES

        many = make_pdf([INVOICE[0]] * (MAX_PAGES + 1))
        assert read_text_layer(many) is None
        fallback = FakeEngine()
        TextLayerFirst(fallback).read(many, "application/pdf")
        assert fallback.calls

    def test_its_name_says_what_it_wraps(self):
        assert TextLayerFirst(FakeEngine()).name == "pdf-text+fake"


class TestThroughThePipeline:
    def test_a_digital_pdf_is_processed_with_no_ocr_at_all(self, session):
        """Upload to ocr_done with the OCR engine forbidden from running."""
        from core import repository
        from core.models import OcrResult
        from core.status import DocStatus
        from worker import run as worker

        document, _ = repository.ingest(
            session, data=make_pdf(INVOICE), mime_type="application/pdf",
            original_filename="inv.pdf",
        )
        worker.run_ocr_job(
            session, worker.claim(session, me="t"), TextLayerFirst(NeverCalled())
        )

        session.refresh(document)
        assert document.status == DocStatus.OCR_DONE
        result = session.query(OcrResult).one()
        assert result.engine == "pdf-text"
        assert "143.39" in result.full_text
        assert result.layout_blocks  # boxes stored, so the extractor gets rows
