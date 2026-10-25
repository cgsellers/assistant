"""
The admin Documents pages, and the original-file endpoint behind them.

Documents go through the real worker with a fake model, so each page shows
what the pipeline actually recorded -- the timeline, the routing scores, the
model calls -- rather than rows written to look like it.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from api.main import app
from core import repository, users
from core.models import DocumentEvent, Job, OcrResult
from core.status import DocStatus, JobKind
from doctypes import registry, seed
from worker import run as worker

from .conftest import TINY_PNG, FakeExtractor, demo_org_id

pytestmark = pytest.mark.usefixtures("fast_hashing")

PASSWORD = "correct horse battery"
INVOICE_TEXT = (
    "ACME LTD\nINVOICE INV-2041\nDue Date: 13 April 2026\nBill To: Northwind\n"
    "Subtotal 100.00\nVAT 20.00"
)
INVOICE_DATA = {
    "vendor": "ACME LTD", "currency": "GBP", "subtotal": "100.00",
    "tax_amount": "20.00", "total": None,
}


@pytest.fixture(autouse=True)
def _seeded(session_factory):
    registry.clear_cache()
    with session_factory() as s:
        seed.sync(s, seed.load_seeds())
        users.create(
            s, "ana@example.com", PASSWORD, role="admin", organisation_id=demo_org_id(s),
            is_staff=True,
        )
    yield
    registry.clear_cache()


@pytest.fixture
def browser(_wired):
    with TestClient(app, base_url="https://testserver") as c:
        c.post("/panel/login", data={"email": "ana@example.com", "password": PASSWORD})
        yield c


@pytest.fixture
def processed(session):
    """A document taken through the extract stage with `data` as the model's answer."""
    counter = iter(range(10_000))

    def _run(text: str, data: dict | None, *, filename: str = "doc.png",
             content: bytes | None = None, mime: str = "image/png"):
        payload = content or TINY_PNG + bytes([next(counter) % 256]) * (next(counter) + 1)
        document, _ = repository.ingest(
            session,
            organisation_id=demo_org_id(session),
            data=payload, mime_type=mime, original_filename=filename
        )
        session.query(Job).filter_by(document_id=document.id).delete()
        document.status = DocStatus.OCR_DONE
        session.add(DocumentEvent(
            document_id=document.id, from_status=DocStatus.UPLOADED,
            to_status=DocStatus.OCR_DONE, detail={"engine": "fake"},
        ))
        session.add(OcrResult(
            document_id=document.id, engine="fake", engine_version="1", params={},
            full_text=text, layout_blocks=[], mean_confidence=0.97,
        ))
        session.add(Job(kind=JobKind.EXTRACT, document_id=document.id))
        session.commit()
        if data is not None:
            worker.run_extract_job(
                session, worker.claim(session, me="test"), FakeExtractor(data)
            )
        return document.id

    return _run


class TestTheList:
    def test_it_shows_each_document_with_its_outcome_amount_and_cost(self, browser, processed):
        processed(INVOICE_TEXT, INVOICE_DATA, filename="acme-2041.pdf")
        page = browser.get("/panel/documents").text

        assert "acme-2041.pdf" in page
        assert '<span class="badge done">done</span>' in page
        assert "invoice <span class=\"muted\">v1</span>" in page
        assert "120.00 GBP" in page
        assert "$0.0012" in page

    def test_it_filters_by_outcome(self, browser, processed):
        processed(INVOICE_TEXT, INVOICE_DATA, filename="fine.pdf")
        processed("CAFE LUNA\nTICKET 7\nIVA 0,75\nTOTAL", {"total": None}, filename="flagged.png")

        page = browser.get("/panel/documents?outcome=needs_review").text
        assert "flagged.png" in page
        assert "fine.pdf" not in page

    def test_it_filters_by_processor(self, browser, processed):
        processed(INVOICE_TEXT, INVOICE_DATA, filename="inv.pdf")
        processed(
            "CAFE LUNA\nTICKET 7\nIVA 0,75\nTOTAL 8,30", {"total": "8,30"}, filename="rec.png"
        )

        page = browser.get("/panel/documents?processor=receipt").text
        assert "rec.png" in page and "inv.pdf" not in page

    def test_older_pages_follow_the_last_row(self, browser, processed, monkeypatch):
        import panel.routes

        monkeypatch.setattr(panel.routes, "PAGE_SIZE", 2)
        for name in ("one.png", "two.png", "three.png"):
            processed(INVOICE_TEXT, None, filename=name)

        first = browser.get("/panel/documents").text
        assert "three.png" in first and "two.png" in first and "one.png" not in first
        older = first.split('href="/panel/documents?')[-1].split('"')[0].replace("&amp;", "&")
        second = browser.get("/panel/documents?" + older).text
        assert "one.png" in second and "three.png" not in second

    def test_an_uploaders_filename_is_text_not_markup(self, browser, processed):
        processed(INVOICE_TEXT, None, filename="<script>alert(1)</script>.png")
        page = browser.get("/panel/documents").text
        assert "<script>alert(1)</script>" not in page
        assert "&lt;script&gt;" in page

    def test_signed_out_is_sent_to_sign_in(self, _wired):
        with TestClient(app, base_url="https://testserver") as anon:
            response = anon.get("/panel/documents", follow_redirects=False)
        assert response.headers["location"] == "/panel/login"


class TestOneDocument:
    def test_it_shows_the_result_beside_the_original(self, browser, processed):
        doc = processed(INVOICE_TEXT, INVOICE_DATA)
        page = browser.get(f"/panel/documents/{doc}").text

        assert f'<img src="/documents/{doc}/file"' in page
        assert "<strong>120.00 GBP</strong> paid to ACME LTD" in page
        assert "amount derived" in page
        assert "the model read None" in page  # the derived total's tooltip

    def test_the_timeline_is_every_stage_in_order(self, browser, processed):
        doc = processed(INVOICE_TEXT, INVOICE_DATA)
        page = browser.get(f"/panel/documents/{doc}").text
        stages = ["uploaded", "ocr done", "classified", "extracted", "validated", "committed"]
        positions = [page.index(f"<strong>{s}</strong>") for s in stages]
        assert positions == sorted(positions)

    def test_routing_records_every_processor_not_just_the_winner(self, browser, processed):
        doc = processed(INVOICE_TEXT, INVOICE_DATA)
        page = browser.get(f"/panel/documents/{doc}").text
        assert '<tr class="winner"><td>invoice</td>' in page
        assert "<td>receipt</td>" in page

    def test_an_unsupported_document_still_shows_why_nothing_matched(
        self, browser, processed, session
    ):
        doc = processed("BOARDING PASS  GATE 14  SEAT 22C", None)
        worker.run_extract_job(session, worker.claim(session, me="t"), FakeExtractor({}))
        page = browser.get(f"/panel/documents/{doc}").text

        assert '<span class="badge unsupported">unsupported</span>' in page
        assert "<td>invoice</td><td>0</td>" in page
        assert "<td>receipt</td><td>0</td>" in page

    def test_model_calls_jobs_and_the_ocr_text_are_listed(self, browser, processed):
        doc = processed(INVOICE_TEXT, INVOICE_DATA)
        page = browser.get(f"/panel/documents/{doc}").text
        assert '<span class="badge done">current</span>' in page
        assert "claude-haiku-4-5-20251001" in page
        assert "<td>extract</td>" in page
        assert "INVOICE INV-2041" in page

    def test_problems_are_listed_for_a_document_in_review(self, browser, processed):
        doc = processed("CAFE LUNA\nTICKET 7\nIVA 0,75\nTOTAL", {"vendor": "CAFE LUNA"})
        page = browser.get(f"/panel/documents/{doc}").text
        assert "Why it needs review" in page
        assert "total" in page.split("Why it needs review")[1].split("</ul>")[0]

    def test_a_pdf_is_embedded_and_a_heic_is_offered_as_a_link(self, browser, processed):
        pdf = processed(INVOICE_TEXT, None, content=b"%PDF-1.4 x", mime="application/pdf")
        heic = processed(INVOICE_TEXT, None, content=b"....ftypheic", mime="image/heic")
        assert f'<iframe src="/documents/{pdf}/file"' in browser.get(f"/panel/documents/{pdf}").text
        heic_page = browser.get(f"/panel/documents/{heic}").text
        assert "can't be shown in the browser" in heic_page
        assert f'href="/documents/{heic}/file"' in heic_page

    def test_an_unknown_document_is_a_page(self, browser):
        response = browser.get("/panel/documents/nope")
        assert response.status_code == 404
        assert "No such document" in response.text


class TestTheOriginalFile:
    def test_it_is_the_bytes_as_uploaded_with_safe_headers(self, client, session):
        document, _ = repository.ingest(
            session,
            organisation_id=demo_org_id(session),
            data=TINY_PNG, mime_type="image/png", original_filename="r.png"
        )
        response = client.get(f"/documents/{document.id}/file")

        assert response.status_code == 200
        assert response.content == TINY_PNG
        assert response.headers["content-type"] == "image/png"
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["cache-control"] == "private, no-store"
        assert response.headers["content-disposition"].startswith('inline; filename="r.png"')

    def test_a_hostile_filename_cannot_break_out_of_the_header(self, client, session):
        name = 'evil"\r\nX-Injected: yes.png'
        document, _ = repository.ingest(
            session,
            organisation_id=demo_org_id(session),
            data=TINY_PNG, mime_type="image/png", original_filename=name
        )
        response = client.get(f"/documents/{document.id}/file")
        assert "x-injected" not in response.headers
        assert 'filename="evil___X-Injected: yes.png"' in response.headers["content-disposition"]

    def test_it_needs_credentials(self, anon_client):
        assert anon_client.get("/documents/anything/file").status_code == 401

    def test_unknown_and_missing_files_are_404s(self, client, session, settings):
        assert client.get("/documents/nope/file").status_code == 404
        document, _ = repository.ingest(
            session,
            organisation_id=demo_org_id(session),
            data=TINY_PNG, mime_type="image/png", original_filename="r.png"
        )
        (settings.storage_dir / document.storage_key).unlink()
        response = client.get(f"/documents/{document.id}/file")
        assert response.status_code == 404
        assert "missing from storage" in response.json()["detail"]


class TestByOrganisation:
    def test_the_list_shows_and_filters_by_organisation(self, browser, processed, session):
        from core import tenancy

        acme = tenancy.create(session, "Acme Ltd", "acme")
        processed(INVOICE_TEXT, None, filename="team-upload.png")
        document, _ = repository.ingest(
            session, organisation_id=acme.id, data=b"acme-bytes", mime_type="image/png",
            original_filename="acme-upload.png",
        )

        everything = browser.get("/panel/documents").text
        assert "<td>Ledgerline demo</td>" in everything and "<td>Acme Ltd</td>" in everything

        team_only = browser.get(
            f"/panel/documents?organisation={tenancy.demo_organisation(session).id}"
        ).text
        assert "team-upload.png" in team_only and "acme-upload.png" not in team_only

        assert "<dt>Organisation</dt><dd>Acme Ltd</dd>" in browser.get(
            f"/panel/documents/{document.id}"
        ).text
