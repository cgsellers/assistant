"""
People correcting what was read, or approving it as it is: PATCH
/documents/{id}/fields (DESIGN §9.4; doctypes/review.py).

A correction runs the document through its processor again, so each test
checks what everything downstream then says -- the page, the list, the
ledger, /result -- and not only the answer to the PATCH.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from core import repository
from core.models import Correction
from doctypes import registry
from doctypes.definition import Definition

from .conftest import TINY_PNG, demo_org_id
from .test_document_fields import READ, RECEIPT, by_name, words
from .test_document_fields import read as _read_fixture
from .test_extract_job import _shared

read = _read_fixture

INVOICE = {
    "name": "invoice",
    "display_name": "Invoice",
    "matches": {"keywords": ["total"], "min_score": 0.3},
    "fields": {
        "vendor": {"type": "string", "description": "Who sent it."},
        "subtotal": {"type": "money", "description": "Before tax."},
        "tax": {"type": "money", "description": "The tax."},
        "total": {"type": "money", "description": "The amount due.", "required": True},
    },
    "transform": {
        "code": (
            "from decimal import Decimal\n"
            "def transform(data):\n"
            "    if data['total'] is None and data['subtotal'] is not None:\n"
            "        tax = Decimal(data['tax'] or '0')\n"
            "        return {**data, 'total': str(Decimal(data['subtotal']) + tax)}\n"
            "    return data\n"
        ),
        "examples": [
            {
                "input": {"subtotal": "10.00", "tax": "2.10", "total": None},
                "expected": {"total": "12.10"},
            }
        ],
    },
    "canonical": {"direction": "out", "amount": "total", "counterparty": "vendor"},
}


@pytest.fixture
def processor(session):
    """The processor documents are read with: the receipt, unless a test says otherwise."""

    def _use(spec: dict = RECEIPT):
        registry.clear_cache()
        _shared(session, Definition.model_validate(spec))

    yield _use
    registry.clear_cache()


def change(client, document, **body):
    return client.patch(f"/documents/{document.id}/fields", json=body)


def ledger_amount(session, document) -> Decimal | None:
    row = repository.current_transaction(session, document.id)
    return row.amount if row else None


class TestCorrecting:
    def test_a_missing_total_corrected_makes_it_ready_everywhere(
        self, client, session, processor, read
    ):
        processor()
        document = read({**READ, "total": None})
        assert document.status == "needs_review"

        response = change(client, document, revision=1, values={"total": "5.30"})
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["document"]["outcome"] == "done"
        assert body["revision"] == 2
        total = by_name(body)["total"]
        assert total["value"] == "5.30"
        assert total["corrected"]["by"] == "test suite"
        assert total["corrected"]["was"] is None
        assert ["Corrected Total", "test suite"] in [[h["what"], h["by"]] for h in body["history"]]

        # Everything downstream agrees: the ledger, the list, what an ERP imports.
        assert ledger_amount(session, document) == Decimal("5.30")
        [row] = client.get("/documents").json()["items"]
        assert row["total"] == {"amount": "5.30", "currency": "EUR"} and row["outcome"] == "done"
        assert client.get(f"/documents/{document.id}/result").json()["data"]["total"] == "5.30"

    def test_a_correction_is_recorded_with_what_was_there_before(
        self, client, session, processor, read
    ):
        processor()
        document = read(READ)
        change(client, document, revision=1, values={"vendor": "Café Luna"})
        [correction] = session.query(Correction).filter_by(document_id=document.id).all()
        assert correction.changes == {"vendor": {"was": "CAFE LUNA", "now": "Café Luna"}}
        assert (correction.by, correction.revision, correction.approved) == ("test suite", 2, False)

    def test_corrected_twice_still_remembers_what_was_read(self, client, processor, read):
        processor()
        document = read(READ)
        change(client, document, revision=1, values={"vendor": "Cafe Luna"})
        body = change(client, document, revision=2, values={"vendor": "Café Luna"}).json()
        assert by_name(body)["vendor"]["corrected"]["was"] == "CAFE LUNA"

    def test_a_corrected_list_is_replaced_whole(self, client, processor, read):
        processor()
        document = read(READ)
        rows = [{"name": "Cappuccino", "amount": "3.20"}, {"name": "Croissant", "amount": "2.10"}]
        rows.append({"name": "Water", "amount": "1.00"})
        body = change(client, document, revision=1, values={"line_items": rows}).json()
        items = by_name(body)["line_items"]
        assert [r["name"] for r in items["rows"]] == ["Cappuccino", "Croissant", "Water"]
        assert items["corrected"]["was"] is None
        # Rows that were printed keep their places; the added one has none.
        assert items["row_locations"][2] is None


class TestTheProcessorRunsAgain:
    def test_a_corrected_subtotal_works_the_total_out_again(self, client, processor, read):
        processor(INVOICE)
        document = read({"vendor": "Fabrikam", "subtotal": "10.00", "tax": "2.10", "total": None})
        assert by_name(client.get(f"/documents/{document.id}/fields").json())["total"]["derived"]

        body = change(client, document, revision=1, values={"subtotal": "20.00"}).json()
        fields = by_name(body)
        assert fields["total"]["value"] == "22.10"
        assert fields["total"]["derived"] is True
        assert fields["total"]["corrected"] is None
        assert fields["subtotal"]["corrected"]["was"] == "10.00"

    def test_a_total_typed_in_stays_as_typed(self, client, processor, read):
        processor(INVOICE)
        document = read({"vendor": "Fabrikam", "subtotal": "10.00", "tax": "2.10", "total": None})
        body = change(client, document, revision=1, values={"total": "12.50"}).json()
        assert by_name(body)["total"]["value"] == "12.50"
        assert by_name(body)["total"]["derived"] is False


class TestReviewAndApproval:
    def test_a_problem_left_keeps_it_in_review_until_approved(
        self, client, session, processor, read
    ):
        processor()
        # No vendor: a rule with its own message holds it in review.
        document = read({**READ, "vendor": None})
        body = change(client, document, revision=1, values={"currency": "EUR"}).json()
        assert body["document"]["outcome"] == "needs_review"
        assert body["problems"] == ["Every receipt names its shop."]
        assert ledger_amount(session, document) is None

        body = change(client, document, revision=2, values={}, approve=True).json()
        assert body["document"]["outcome"] == "done"
        assert body["problems"] == ["Every receipt names its shop."]  # still on record
        assert ["Approved as it is", "test suite"] in [
            [h["what"], h["by"]] for h in body["history"]
        ]
        assert ledger_amount(session, document) == Decimal("5.30")

    def test_an_approval_holds_only_until_the_next_correction(
        self, client, session, processor, read
    ):
        processor()
        document = read({**READ, "vendor": None})
        change(client, document, revision=1, values={}, approve=True)
        body = change(client, document, revision=2, values={"currency": "GBP"}).json()
        assert body["document"]["outcome"] == "needs_review"
        # And the ledger does not keep a figure its own client has disputed.
        assert ledger_amount(session, document) is None

    def test_a_ready_document_corrected_into_a_problem_leaves_the_ledger(
        self, client, session, processor, read
    ):
        processor()
        document = read(READ)
        assert ledger_amount(session, document) == Decimal("5.30")
        body = change(client, document, revision=1, values={"total": "-1.00"}).json()
        assert body["document"]["outcome"] == "needs_review"
        assert by_name(body)["total"]["problem"] == "Total must be greater than 0, got -1.00"
        assert ledger_amount(session, document) is None
        assert body["history"][-1]["what"] == "Held for review"


class TestRefusals:
    def test_a_change_made_against_an_old_revision_is_refused(self, client, processor, read):
        processor()
        document = read(READ)
        assert change(client, document, revision=1, values={"vendor": "First"}).status_code == 200
        late = change(client, document, revision=1, values={"vendor": "Second"})
        assert late.status_code == 409
        assert late.json()["detail"] == "Someone else changed this document since you opened it."
        body = client.get(f"/documents/{document.id}/fields").json()
        assert by_name(body)["vendor"]["value"] == "First" and body["revision"] == 2

    def test_values_that_do_not_fit_are_named_and_nothing_changes(self, client, processor, read):
        processor()
        document = read(READ)
        response = change(
            client,
            document,
            revision=1,
            values={
                "total": "12.345.6",
                "issued_on": "24/10/2026",
                "nonsense": "x",
                "line_items": [{"name": "Tea", "colour": "green"}],
            },
        )
        assert response.status_code == 422
        assert response.json() == {
            "detail": "Some values don't fit their fields.",
            "fields": {
                "total": 'An amount, like "1234.56"',
                "issued_on": "A date, YYYY-MM-DD",
                "nonsense": "No such field",
                "line_items": "Row 1: no column called 'colour'",
            },
        }
        assert client.get(f"/documents/{document.id}/fields").json()["revision"] == 1

    def test_a_document_not_yet_read_cannot_be_corrected(self, client, session, processor):
        processor()
        document, _ = repository.ingest(
            session,
            organisation_id=demo_org_id(session),
            data=TINY_PNG,
            mime_type="image/png",
            original_filename=None,
        )
        response = change(client, document, revision=1, values={"total": "1.00"})
        assert response.status_code == 409
        assert response.json()["detail"] == "Nothing has been read from this document yet."

    def test_a_missing_body_is_refused_after_the_document_is_found(self, client, processor, read):
        processor()
        document = read(READ)
        assert client.patch(f"/documents/{document.id}/fields").status_code == 422
        assert client.patch("/documents/no-such/fields").status_code == 404


def test_the_page_words_used_here_are_printed():
    """A guard on this module's fixtures: the receipt's words are the ones other tests read."""
    assert any(b.text == "TOTAL" for b in words((600, "TOTAL EUR 5,30")))


class TestFromTheBrowser:
    def test_a_signed_in_change_needs_its_csrf_token(
        self, _wired, session, processor, read, fast_hashing
    ):
        from fastapi.testclient import TestClient

        from api.main import app
        from core import users

        processor()
        document = read(READ)
        users.create(
            session, "bo@ledgerline.test", "correct horse battery",
            organisation_id=demo_org_id(session), is_staff=False, role="author",
        )  # fmt: skip
        with TestClient(app, base_url="https://testserver") as browser:
            me = browser.post(
                "/auth/login",
                json={"email": "bo@ledgerline.test", "password": "correct horse battery"},
            ).json()
            path = f"/documents/{document.id}/fields"
            body = {"revision": 1, "values": {"vendor": "Café Luna"}}
            assert browser.patch(path, json=body).status_code == 403
            answer = browser.patch(path, json=body, headers={"X-CSRF-Token": me["csrf_token"]})
            assert answer.status_code == 200
            assert by_name(answer.json())["vendor"]["corrected"]["by"] == "bo@ledgerline.test"
