"""
The documents list a client sees: GET /documents, /summary, /types and
/export (DESIGN §9.4), against the contract in portal/src/api/contract.ts.

Isolation between clients is not repeated here: every one of these routes is
classified `client_listing` in test_every_route.py, which crosses each with
another client's key.
"""

from __future__ import annotations

import csv
import io
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from core import repository
from core.models import Document
from core.status import DocStatus
from doctypes import listing, registry
from worker import run as worker

from . import test_extract_job as extract_job
from .conftest import TINY_PNG, FakeExtractor, demo_org_id, receipt_definition
from .test_extract_job import _ledger_receipt, _shared, claim_extract

# The extract stage's fixtures, for the tests that run the worker.
ready = extract_job.ready
_cold_registry = extract_job._cold_registry

NOW = datetime(2026, 10, 26, 17, 40, tzinfo=UTC)


@pytest.fixture
def make(session):
    """A document of the demo organisation, as the list will see it."""
    made = {"n": 0}

    def _make(
        *,
        status: str = DocStatus.COMMITTED,
        hours_ago: float = 0,
        supplier: str | None = "Globe Logistics",
        document_date: date | None = date(2026, 10, 24),
        total: str | None = "91.19",
        currency: str | None = "EUR",
        doc_type: str | None = "receipt",
        filename: str = "a.pdf",
    ) -> Document:
        made["n"] += 1
        document, _ = repository.ingest(
            session,
            organisation_id=demo_org_id(session),
            data=TINY_PNG + str(made["n"]).encode(),
            mime_type="image/png",
            original_filename=filename,
        )
        document.status = status
        document.uploaded_at = NOW - timedelta(hours=hours_ago)
        document.supplier = supplier
        document.document_date = document_date
        document.total_amount = Decimal(total) if total is not None else None
        document.total_currency = currency
        document.doc_type = doc_type
        session.commit()
        return document

    return _make


@pytest.fixture
def labelled(session):
    """The receipt processor, with the name people see."""
    registry.clear_cache()
    _shared(session, receipt_definition(display_name="Receipt"))
    yield
    registry.clear_cache()


def ids(response) -> list[str]:
    assert response.status_code == 200, response.text
    return [row["id"] for row in response.json()["items"]]


class TestTheList:
    def test_a_row_is_what_the_portal_expects(self, client, make, labelled):
        document = make(filename="globe-inv-7701.pdf")
        [row] = client.get("/documents").json()["items"]
        assert row == {
            "id": document.id,
            "filename": "globe-inv-7701.pdf",
            "received_at": "2026-10-26T17:40:00Z",
            "document_date": "2026-10-24",
            "supplier": "Globe Logistics",
            "type": {"name": "receipt", "label": "Receipt"},
            "total": {"amount": "91.19", "currency": "EUR"},
            "outcome": "done",
        }

    def test_a_document_in_review_shows_what_was_read_and_gaps_as_null(self, client, make):
        make(status=DocStatus.NEEDS_REVIEW, total=None, document_date=None)
        [row] = client.get("/documents").json()["items"]
        assert row["outcome"] == "needs_review"
        assert row["supplier"] == "Globe Logistics"
        assert row["total"] is None and row["document_date"] is None

    def test_newest_first(self, client, make):
        old, new = make(hours_ago=5), make(hours_ago=1)
        assert ids(client.get("/documents")) == [new.id, old.id]

    def test_filters(self, client, make):
        review = make(status=DocStatus.NEEDS_REVIEW, supplier="Contoso Cloud", hours_ago=1)
        invoice = make(
            doc_type="invoice", supplier="Fabrikam", filename="fab-7704.pdf", hours_ago=2
        )
        plain = make(supplier="Coop Italia", hours_ago=3)

        assert ids(client.get("/documents", params={"outcome": "needs_review"})) == [review.id]
        assert ids(client.get("/documents", params={"type": "invoice"})) == [invoice.id]
        # Supplier or file name, either case.
        assert ids(client.get("/documents", params={"q": "COOP"})) == [plain.id]
        assert ids(client.get("/documents", params={"q": "fab-77"})) == [invoice.id]

    def test_search_text_is_text_not_a_pattern(self, client, make):
        make(supplier="100% Organic", hours_ago=1)
        make(supplier="1000 Things", hours_ago=2)
        assert len(ids(client.get("/documents", params={"q": "100%"}))) == 1
        assert ids(client.get("/documents", params={"q": "_"})) == []

    def test_received_days_are_the_clients_own(self, client, make):
        # 23:30 UTC on the 25th is 00:30 on the 26th in Madrid.
        late = make(hours_ago=18 + 1 / 6)
        assert late.uploaded_at == datetime(2026, 10, 25, 23, 30, tzinfo=UTC)
        day = {"from": "2026-10-26", "to": "2026-10-26"}
        assert ids(client.get("/documents", params={**day, "tz": "Europe/Madrid"})) == [late.id]
        assert ids(client.get("/documents", params=day)) == []

    def test_an_unknown_time_zone_is_refused(self, client):
        response = client.get("/documents", params={"tz": "Mars/Olympus_Mons"})
        assert response.status_code == 422
        assert "Unknown time zone" in response.json()["detail"]


class TestPaging:
    def test_pages_cover_everything_once(self, client, make):
        made = [make(hours_ago=h).id for h in range(7)]
        seen, cursor = [], None
        while True:
            page = client.get(
                "/documents", params={"limit": 3, **({"cursor": cursor} if cursor else {})}
            )
            seen += ids(page)
            cursor = page.json()["next_cursor"]
            if cursor is None:
                break
        assert seen == made

    def test_a_document_arriving_between_pages_does_not_shift_them(self, client, make):
        made = [make(hours_ago=h + 1).id for h in range(4)]
        first = client.get("/documents", params={"limit": 2}).json()
        make(hours_ago=0)  # the newest of all, arriving while someone reads
        second = client.get("/documents", params={"limit": 2, "cursor": first["next_cursor"]})
        assert ids(second) == made[2:]

    def test_by_document_date_newest_first_and_undated_last_across_pages(self, client, make):
        undated = make(document_date=None, hours_ago=0)
        oldest = make(document_date=date(2026, 9, 1), hours_ago=1)
        newest = make(document_date=date(2026, 10, 20), hours_ago=2)
        tie_late, tie_early = (
            make(document_date=date(2026, 10, 1), hours_ago=3),
            make(document_date=date(2026, 10, 1), hours_ago=4),
        )
        seen, cursor = [], None
        while True:
            params = {"sort": "document_date", "limit": 2, **({"cursor": cursor} if cursor else {})}
            page = client.get("/documents", params=params)
            seen += ids(page)
            cursor = page.json()["next_cursor"]
            if cursor is None:
                break
        assert seen == [newest.id, tie_late.id, tie_early.id, oldest.id, undated.id]

    @pytest.mark.parametrize("cursor", ["not-a-cursor", "eyJzIjogIng"])
    def test_a_cursor_it_did_not_make_is_refused(self, client, cursor):
        assert client.get("/documents", params={"cursor": cursor}).status_code == 400

    def test_a_cursor_from_another_sort_is_refused(self, client, make):
        for h in range(3):
            make(hours_ago=h)
        cursor = client.get("/documents", params={"limit": 1}).json()["next_cursor"]
        response = client.get("/documents", params={"cursor": cursor, "sort": "document_date"})
        assert response.status_code == 400
        assert "another sort" in response.json()["detail"]


class TestSummary:
    def test_counts_by_outcome_and_totals_per_currency(self, client, make):
        make(total="10.10", currency="EUR")
        make(total="5.05", currency="EUR", status=DocStatus.NEEDS_REVIEW)
        make(total="7.00", currency="GBP")
        make(total=None, status=DocStatus.NEEDS_REVIEW)
        make(total=None, currency=None, status=DocStatus.UPLOADED)

        body = client.get("/documents/summary").json()
        assert body == {
            "documents": 5,
            "by_outcome": {
                "processing": 1,
                "done": 2,
                "needs_review": 2,
                "unsupported": 0,
                "failed": 0,
            },
            # Never one figure across currencies.
            "totals": [
                {"amount": "15.15", "currency": "EUR"},
                {"amount": "7.00", "currency": "GBP"},
            ],
        }

    def test_follows_the_filters(self, client, make):
        make(total="10.10", currency="EUR")
        make(total="5.05", currency="EUR", status=DocStatus.NEEDS_REVIEW)
        body = client.get("/documents/summary", params={"outcome": "needs_review"}).json()
        assert body["documents"] == 1
        assert body["totals"] == [{"amount": "5.05", "currency": "EUR"}]


class TestTypesAndExport:
    def test_types_are_the_kinds_this_client_has_with_their_labels(self, client, make, labelled):
        make(doc_type="receipt")
        make(doc_type="receipt")
        make(doc_type="supplier_statement")
        make(doc_type=None, status=DocStatus.UPLOADED)
        assert client.get("/documents/types").json() == [
            {"name": "receipt", "label": "Receipt"},
            {"name": "supplier_statement", "label": "Supplier statement"},
        ]

    def test_export_is_the_filtered_list_as_csv(self, client, make, labelled):
        make(supplier="Coop Italia", total="41.38", hours_ago=1)
        make(supplier="Elsewhere", status=DocStatus.NEEDS_REVIEW, hours_ago=2)
        response = client.get("/documents/export", params={"outcome": "done"})
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/csv")
        assert response.headers["content-disposition"].startswith(
            'attachment; filename="ledgerline-documents-'
        )
        rows = list(csv.reader(io.StringIO(response.text)))
        assert rows[0] == [
            "Received",
            "Document date",
            "Supplier",
            "Type",
            "Total",
            "Currency",
            "Status",
            "File",
        ]
        assert rows[1:] == [
            [
                "2026-10-26T16:40:00+00:00",
                "2026-10-24",
                "Coop Italia",
                "Receipt",
                "41.38",
                "EUR",
                "done",
                "a.pdf",
            ]
        ]

    def test_text_a_spreadsheet_would_run_is_defused(self, client, make):
        make(supplier='=HYPERLINK("http://evil.test","Refund")', filename="+cmd.pdf")
        rows = list(csv.reader(io.StringIO(client.get("/documents/export").text)))
        assert rows[1][2].startswith("'=")
        assert rows[1][7] == "'+cmd.pdf"


class TestFillingTheColumns:
    def test_a_document_sent_to_review_still_shows_what_was_read(self, session, ready):
        _shared(session, _ledger_receipt())
        document, _ = ready()
        worker.run_extract_job(
            session,
            claim_extract(session),
            # A negative total fails the receipt's `positive` rule.
            FakeExtractor({"total": "-9,35", "vendor": "CAFE LUNA", "issued_on": "2026-05-12"}),
        )
        session.refresh(document)
        assert document.status == DocStatus.NEEDS_REVIEW
        assert (document.doc_type, document.supplier, document.document_date) == (
            "receipt",
            "CAFE LUNA",
            date(2026, 5, 12),
        )
        assert document.total_amount == Decimal("-9.35")

    def test_a_committed_document_shows_what_its_ledger_row_says(self, session, ready):
        _shared(session, _ledger_receipt())
        document, _ = ready()
        worker.run_extract_job(
            session,
            claim_extract(session),
            FakeExtractor({"total": "9,35", "vendor": "CAFE LUNA", "issued_on": "2026-05-12"}),
        )
        session.refresh(document)
        ledger = repository.current_transaction(session, document.id)
        assert document.status == DocStatus.COMMITTED
        assert (document.supplier, document.document_date, document.total_amount) == (
            ledger.counterparty,
            ledger.occurred_on,
            ledger.amount,
        )

    def test_backfill_fills_documents_read_before_the_columns_existed(self, session, ready):
        _shared(session, _ledger_receipt())
        document, _ = ready()
        worker.run_extract_job(
            session,
            claim_extract(session),
            FakeExtractor({"total": "9,35", "vendor": "CAFE LUNA", "issued_on": "2026-05-12"}),
        )
        document.doc_type = document.supplier = document.total_amount = None
        session.commit()

        assert listing.backfill(session) == 1
        session.refresh(document)
        assert (document.doc_type, document.supplier, document.total_amount) == (
            "receipt",
            "CAFE LUNA",
            Decimal("9.35"),
        )
        assert listing.backfill(session) == 0
