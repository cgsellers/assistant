"""
GET /documents/{id}/result -- what a client system imports.

Documents are taken through the real worker with a fake model, so each
outcome is the one the pipeline actually produces rather than rows written
to look like it.
"""

from __future__ import annotations

import pytest

from core import repository
from core.models import Job, OcrResult
from core.status import DocStatus, JobKind
from doctypes import registry, seed
from doctypes.results import OUTCOMES
from worker import run as worker

from .conftest import TINY_PNG, FakeExtractor

INVOICE_TEXT = (
    "ACME LTD\nINVOICE INV-2041\nDue Date: 13 April 2026\nBill To: Northwind\n"
    "Subtotal 100.00\nVAT 20.00"
)
RECEIPT_TEXT = "CAFE LUNA\nTICKET 0042\nIVA 10% 0,75\nTOTAL 8,30\nTARJETA"


@pytest.fixture(autouse=True)
def _seeded(session):
    registry.clear_cache()
    seed.sync(session, seed.load_seeds())
    yield
    registry.clear_cache()


@pytest.fixture
def processed(session):
    """Upload-and-OCR a document, then run its extract job with `data` as the model's answer."""

    def _run(text: str, data: dict | None, *, content: bytes = TINY_PNG):
        document, _ = repository.ingest(
            session, data=content, mime_type="image/png", original_filename="d.png"
        )
        session.query(Job).delete()
        document.status = DocStatus.OCR_DONE
        session.add(OcrResult(
            document_id=document.id, engine="fake", engine_version="1", params={},
            full_text=text, layout_blocks=[], mean_confidence=None,
        ))
        session.add(Job(kind=JobKind.EXTRACT, document_id=document.id))
        session.commit()
        if data is not None:
            worker.run_extract_job(
                session, worker.claim(session, me="test"), FakeExtractor(data)
            )
        return document.id

    return _run


def _result(client, document_id):
    response = client.get(f"/documents/{document_id}/result")
    assert response.status_code == 200
    return response.json()


def test_a_derived_total_is_in_data_and_marked(client, processed):
    doc = processed(INVOICE_TEXT, {
        "vendor": "ACME LTD", "currency": "GBP", "subtotal": "100.00",
        "tax_amount": "20.00", "total": None,
    })
    result = _result(client, doc)

    assert result["outcome"] == "done"
    assert result["processor"] == {"name": "invoice", "version": 1, "profile": None}
    assert result["data"]["total"] == "120.00"
    assert result["read"]["total"] is None  # what the page said
    assert result["derived"] == ["total"]
    assert result["transaction"]["amount"] == "120.00"
    assert result["transaction"]["derived"] == ["amount"]


def test_every_declared_field_is_present_even_when_null(client, processed):
    doc = processed(INVOICE_TEXT, {"subtotal": "100.00", "tax_amount": "20.00"})
    data = _result(client, doc)["data"]
    assert "po_number" in data and data["po_number"] is None
    assert "line_items" in data


def test_values_are_normalised_whatever_the_page_printed(client, processed):
    """A Spanish receipt prints 8,30; a client parsing JSON wants 8.30."""
    doc = processed(RECEIPT_TEXT, {
        "vendor": "CAFE LUNA", "total": "8,30", "tax_amount": "0,75",
        "issued_on": "2026-05-12", "currency": "EUR",
    })
    result = _result(client, doc)
    assert result["data"]["total"] == "8.30"
    assert result["data"]["tax_amount"] == "0.75"
    assert result["read"]["total"] == "8,30"
    assert result["data"]["issued_on"] == "2026-05-12"


def test_a_document_in_review_still_returns_its_data_and_why(client, processed):
    doc = processed(RECEIPT_TEXT, {"vendor": "CAFE LUNA", "total": None})
    result = _result(client, doc)

    assert result["outcome"] == "needs_review"
    assert result["data"]["vendor"] == "CAFE LUNA"
    assert any("total" in p for p in result["problems"])
    assert result["transaction"] is None


def test_a_document_still_being_processed_has_no_data_yet(client, processed):
    doc = processed(INVOICE_TEXT, None)  # OCR done, extract job not run
    result = _result(client, doc)
    assert result["outcome"] == "processing"
    assert result["data"] is None
    assert result["processor"] is None


def test_an_unrecognised_document_says_so(client, processed, session):
    doc = processed("BOARDING PASS  GATE 14  SEAT 22C", None)
    worker.run_extract_job(session, worker.claim(session, me="test"), FakeExtractor({}))
    result = _result(client, doc)
    assert result["outcome"] == "unsupported"
    assert result["data"] is None


def test_a_trial_never_shows_as_the_result(client, processed, session):
    from doctypes import authoring

    doc = processed(INVOICE_TEXT, None)
    authoring.trial_run(
        session, "invoice", 1, document_id=doc,
        extractor=FakeExtractor({"total": "999.00"}),
    )
    assert _result(client, doc)["data"] is None


def test_the_version_that_read_it_is_used_not_the_live_one(client, processed, session):
    """Fields are as that version declared them, even after a new one is published."""
    from doctypes import authoring

    doc = processed(INVOICE_TEXT, {"subtotal": "100.00", "tax_amount": "20.00", "total": None})
    base = authoring.get_version(session, "invoice", 1).definition
    renamed = {**base, "fields": {"grand_total" if k == "total" else k: v
                                  for k, v in base["fields"].items()}}
    renamed.pop("transform")
    renamed["rules"] = []
    renamed["canonical"] = {**base["canonical"], "amount": "grand_total"}
    draft = authoring.create_draft(session, renamed, based_on=1, created_by="t")
    authoring.publish(session, "invoice", draft.version, published_by="t")

    data = _result(client, doc)["data"]
    assert "total" in data and "grand_total" not in data


def test_an_unknown_document_is_a_404(client):
    assert client.get("/documents/nope/result").status_code == 404


def test_it_needs_credentials(anon_client):
    assert anon_client.get("/documents/anything/result").status_code == 401


def test_every_status_has_an_outcome():
    """A status added without deciding what a client sees for it fails here."""
    assert set(OUTCOMES) == set(DocStatus)
