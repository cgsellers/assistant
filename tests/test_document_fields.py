"""
One document as the client portal shows it: GET /documents/{id}/fields
(DESIGN §9.4), driven through the worker as a real document would be.

Isolation is covered in test_every_route.py, where the route is classified
`client_document`: another client's document answers exactly as a missing one.
"""

from __future__ import annotations

import pytest

from core import repository
from core.models import OcrResult
from core.status import JobKind
from doctypes import registry
from doctypes.definition import Definition
from doctypes.fields import label
from ocr.base import OcrOutput, PageSize, TextBlock
from worker import run as worker

from .conftest import TINY_PNG, FakeEngine, FakeExtractor, demo_org_id
from .test_extract_job import _shared

RECEIPT = {
    "name": "receipt",
    "display_name": "Receipt",
    "matches": {"keywords": ["total"], "min_score": 0.3},
    "fields": {
        "vendor": {"type": "string", "description": "The shop's name, at the top."},
        "issued_on": {"type": "date", "description": "The date of purchase."},
        "total": {
            "type": "money",
            "description": "The amount paid, labelled TOTAL.",
            "required": True,
        },
        "currency": {"type": "string", "description": "ISO 4217 code."},
        "line_items": {
            "type": "list",
            "description": "What was bought.",
            "item_fields": {
                "name": {"type": "string", "description": "The item."},
                "amount": {"type": "money", "description": "Its price."},
            },
        },
    },
    "rules": [
        # As the seeded receipt has: only a `required` rule owns absence.
        {"type": "required", "field": "total"},
        {"type": "positive", "field": "total"},
        {"type": "required", "field": "vendor", "message": "Every receipt names its shop."},
    ],
    "canonical": {
        "direction": "out",
        "amount": "total",
        "occurred_on": "issued_on",
        "counterparty": "vendor",
        "currency": "currency",
    },
}


def words(*rows: tuple[int, str]) -> list[TextBlock]:
    """Tesseract-like words on a 1000 x 1000 page: each line at y, 10px per character."""
    blocks = []
    for y, text in rows:
        x = 50
        for word in text.split():
            blocks.append(
                TextBlock(text=word, bbox=(x, y, x + 10 * len(word), y + 20), confidence=0.9)
            )
            x += 10 * len(word) + 10
    return blocks


PRINTED = words(
    (100, "CAFE LUNA"),
    (150, "12/05/2026 10:31"),
    (300, "Cappuccino 3,20"),
    (330, "Croissant 2,10"),
    (600, "TOTAL EUR 5,30"),
)

#: The page the words above are on.
A4ISH = (PageSize(1, 1000, 1000),)

READ = {
    "vendor": "CAFE LUNA",
    # The model answers in ISO; the page printed 12/05/2026.
    "issued_on": "2026-05-12",
    "total": "5,30",
    "currency": "EUR",
    "line_items": [
        {"name": "Cappuccino", "amount": "3,20"},
        {"name": "Croissant", "amount": "2,10"},
    ],
}


@pytest.fixture(autouse=True)
def receipt(session):
    registry.clear_cache()
    _shared(session, Definition.model_validate(RECEIPT))
    yield
    registry.clear_cache()


@pytest.fixture
def read(session):
    """A document taken through OCR and extraction by the worker, as a real one is."""

    def _read(answer: dict, *, page_sizes=A4ISH, blocks=PRINTED):
        document, _ = repository.ingest(
            session,
            organisation_id=demo_org_id(session),
            data=TINY_PNG + repr(answer).encode(),
            mime_type="image/png",
            original_filename="luna.png",
        )
        engine = FakeEngine(
            OcrOutput(
                full_text=" ".join(b.text for b in blocks),
                blocks=list(blocks),
                page_sizes=list(page_sizes),
                engine="fake",
            )
        )
        worker.run_ocr_job(session, _claim(session, JobKind.OCR), engine)
        worker.run_extract_job(session, _claim(session, JobKind.EXTRACT), FakeExtractor(answer))
        session.refresh(document)
        return document

    return _read


def _claim(session, kind):
    while (job := worker.claim(session, me="test")) is not None:
        if job.kind == kind:
            return job
    raise AssertionError(f"no {kind} job queued")


def by_name(body: dict) -> dict[str, dict]:
    return {f["name"]: f for f in body["fields"]}


class TestAReadDocument:
    def test_every_field_in_order_labelled_typed_and_in_wire_form(self, client, read):
        document = read(READ)
        body = client.get(f"/documents/{document.id}/fields").json()

        assert body["processor"] == {"name": "receipt", "label": "Receipt", "version": 1}
        assert body["currency"] == "EUR"
        assert [(f["name"], f["label"], f["type"]) for f in body["fields"]] == [
            ("vendor", "Vendor", "string"),
            ("issued_on", "Issued on", "date"),
            ("total", "Total", "money"),
            ("currency", "Currency", "string"),
            ("line_items", "Line items", "list"),
        ]
        fields = by_name(body)
        assert fields["total"]["value"] == "5.30"
        assert fields["issued_on"]["value"] == "2026-05-12"
        # Required by the field itself, and by a rule.
        assert fields["total"]["required"] and fields["vendor"]["required"]
        assert not fields["currency"]["required"]
        assert fields["line_items"]["columns"] == [
            {"name": "name", "label": "Name", "type": "string"},
            {"name": "amount", "label": "Amount", "type": "money"},
        ]
        assert fields["line_items"]["rows"] == [
            {"name": "Cappuccino", "amount": "3.20"},
            {"name": "Croissant", "amount": "2.10"},
        ]
        assert body["document"]["outcome"] == "done"
        assert body["document"]["mime_type"] == "image/png"
        assert body["problems"] == [] and body["revision"] == 1 and body["read_at"]

    def test_each_value_points_at_where_it_was_printed(self, client, read):
        fields = by_name(client.get(f"/documents/{read(READ).id}/fields").json())
        top = {
            name: (f["location"] or {}).get("box", [None, None])[1]
            for name, f in fields.items()
            if "location" in f
        }
        assert top["vendor"] == pytest.approx(0.096)
        assert top["issued_on"] == pytest.approx(0.146)
        assert top["total"] == pytest.approx(0.596)
        rows = fields["line_items"]["row_locations"]
        assert [r["box"][1] for r in rows] == pytest.approx([0.296, 0.326])
        assert all(f["location"]["page"] == 1 for f in fields.values() if f.get("location"))

    def test_what_happened_in_the_clients_words(self, client, read):
        body = client.get(f"/documents/{read(READ).id}/fields").json()
        assert [h["what"] for h in body["history"]] == [
            "Received",
            "Recognised as receipt",
            "Ready",
            "Added to the ledger",
        ]

    def test_a_value_worked_out_is_marked_and_points_nowhere(self, client, read, session):
        document = read(READ)
        repository.record_extraction(
            session,
            document_id=document.id,
            doc_type="receipt",
            schema_version=1,
            model_used="m",
            raw_json={**READ, "total": None},
            transformed_json=READ,
            derived_fields=["total"],
        )
        total = by_name(client.get(f"/documents/{document.id}/fields").json())["total"]
        assert total["derived"] is True
        assert total["value"] == "5.30"
        assert total["location"] is None


class TestADocumentInReview:
    def test_each_problem_is_at_its_field_or_the_documents_own(self, client, read):
        document = read({**READ, "vendor": None, "total": "-5,30"})
        body = client.get(f"/documents/{document.id}/fields").json()
        assert body["document"]["outcome"] == "needs_review"
        assert by_name(body)["total"]["problem"] == "Total must be greater than 0, got -5.30"
        # A rule's own message names no field: it is about the document.
        assert body["problems"] == ["Every receipt names its shop."]
        assert by_name(body)["vendor"]["value"] is None


class TestWhatCannotBeShown:
    def test_a_document_not_yet_read_has_no_fields(self, client, session):
        document, _ = repository.ingest(
            session,
            organisation_id=demo_org_id(session),
            data=TINY_PNG,
            mime_type="image/png",
            original_filename=None,
        )
        body = client.get(f"/documents/{document.id}/fields").json()
        assert body["processor"] is None and body["fields"] == [] and body["read_at"] is None
        assert body["document"]["outcome"] == "processing"
        assert [h["what"] for h in body["history"]] == ["Received"]

    def test_ocr_from_before_page_sizes_has_values_but_no_locations(self, client, read, session):
        document = read(READ, page_sizes=())
        assert session.query(OcrResult).filter_by(document_id=document.id).one().page_sizes is None
        fields = by_name(client.get(f"/documents/{document.id}/fields").json())
        assert fields["total"]["value"] == "5.30"
        assert all(f.get("location") is None for f in fields.values())
        assert fields["line_items"]["row_locations"] == [None, None]

    def test_an_unknown_document_is_not_found(self, client):
        assert client.get("/documents/no-such-document/fields").status_code == 404


class TestTheOcrRecordsPageSizes:
    def test_the_worker_stores_them(self, read, session):
        document = read(READ, page_sizes=(PageSize(1, 2480, 3508),))
        result = session.query(OcrResult).filter_by(document_id=document.id).one()
        assert result.page_sizes == [{"page": 1, "width": 2480, "height": 3508}]


def test_a_problem_reads_as_a_sentence_about_the_document():
    from doctypes.fields import _problems

    definition = Definition.model_validate(RECEIPT)
    by_field, _ = _problems(
        definition,
        [
            "total is required but was not extracted",
            "vendor must say who sold it, and total must be positive",
        ],
    )
    assert by_field["total"] == "Total is required, but wasn't found on the document"
    assert by_field["vendor"] == "Vendor must say who sold it, and total must be positive"


@pytest.mark.parametrize(
    ("name", "shown"),
    [
        ("vendor_tax_id", "Vendor tax ID"),
        ("po_number", "PO number"),
        ("iban", "IBAN"),
        ("issued_on", "Issued on"),
    ],
)
def test_labels_read_like_people_write(name, shown):
    assert label(name) == shown
