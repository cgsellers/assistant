"""
The cost report.

The numbers it exists to separate -- total, wasted, per document -- are easy
to blur, so most of these assert one of them stays distinct from another.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest

from core import repository
from core.costs import cost_report, format_report
from core.models import Extraction, utcnow
from llm.base import ExtractionResult

from .conftest import TINY_PNG


@pytest.fixture
def spend(session):
    """Record calls the way the pipeline records them."""
    documents: dict[str, str] = {}

    def _doc(name: str) -> str:
        if name not in documents:
            document, _ = repository.ingest(
                session,
                data=TINY_PNG + name.encode(),
                mime_type="image/png",
                original_filename=f"{name}.png",
            )
            documents[name] = document.id
        return documents[name]

    def _ok(name, cost, *, model="claude-haiku-4-5-20251001", trial=False, latency=1000,
            doc_type="receipt", tokens=1000):
        return repository.record_extraction(
            session,
            document_id=_doc(name),
            doc_type=doc_type,
            schema_version=1,
            model_used=model,
            raw_json={"total": "1"},
            validation_errors=[],
            is_trial=trial,
            input_tokens=tokens,
            output_tokens=100,
            cost_usd=Decimal(cost),
            latency_ms=latency,
        )

    def _failed(name, cost):
        return repository.record_failed_attempt(
            session,
            document_id=_doc(name),
            doc_type="receipt",
            schema_version=1,
            reason="declined",
            spent=ExtractionResult(
                data={},
                model="claude-haiku-4-5-20251001",
                input_tokens=1000,
                output_tokens=50,
                cost_usd=Decimal(cost),
                latency_ms=900,
            ),
        )

    _ok.failed = _failed
    return _ok


class TestTheThreeNumbers:
    def test_total_includes_failed_and_superseded_calls(self, session, spend):
        """The invoice does not care whether a call was useful."""
        spend("a", "0.002000")
        spend("a", "0.002000")          # a re-extraction; supersedes the first
        spend.failed("b", "0.001500")   # billed, produced nothing

        assert cost_report(session).total_usd == Decimal("0.005500")

    def test_wasted_is_only_the_calls_that_produced_nothing(self, session, spend):
        spend("a", "0.002000")
        spend.failed("b", "0.001500")

        report = cost_report(session)
        assert report.wasted_usd == Decimal("0.001500")
        assert report.failed_calls == 1

    def test_per_document_is_higher_than_per_call_when_documents_retry(
        self, session, spend
    ):
        """Exactly the gap between "a call costs" and "a receipt costs"."""
        spend.failed("a", "0.002000")
        spend("a", "0.002000")
        spend("b", "0.002000")

        report = cost_report(session)
        assert report.per_call_usd == Decimal("0.002000")
        assert report.per_document_usd == Decimal("0.003000")
        assert report.retried_documents == 1

    def test_trials_are_reported_beside_the_total_not_inside_it(self, session, spend):
        spend("a", "0.002000")
        spend("b", "0.009000", trial=True)

        report = cost_report(session)
        assert report.total_usd == Decimal("0.002000")
        assert report.trial_usd == Decimal("0.009000")
        assert report.documents == 1


class TestHonesty:
    def test_an_unpriced_call_is_flagged_not_summed_as_free(self, session, spend):
        spend("a", "0", model="some-model-missing-from-the-table")

        report = cost_report(session)
        assert report.unpriced_calls == 1
        assert "priced at zero" in format_report(report)

    def test_a_call_with_no_tokens_is_not_unpriced(self, session, spend):
        spend("a", "0", tokens=0)
        assert cost_report(session).unpriced_calls == 0

    def test_an_empty_window_says_so(self, session):
        assert cost_report(session).calls == 0
        assert "no model calls" in format_report(cost_report(session))

    def test_money_stays_decimal(self, session, spend):
        spend("a", "0.000100")
        spend("b", "0.000200")
        total = cost_report(session).total_usd
        assert total == Decimal("0.000300")
        assert isinstance(total, Decimal)

    def test_six_places_so_a_fraction_of_a_cent_is_not_rounded_to_nothing(
        self, session, spend
    ):
        spend("a", "0.001205")
        assert cost_report(session).as_dict()["total_usd"] == "0.001205"


class TestSlicing:
    def test_by_model_is_largest_first(self, session, spend):
        spend("a", "0.001000", model="claude-haiku-4-5-20251001")
        spend("b", "0.009000", model="claude-sonnet-5-20260101")

        assert [s.key for s in cost_report(session).by_model][0] == (
            "claude-sonnet-5-20260101"
        )

    def test_by_doc_type(self, session, spend):
        spend("a", "0.001000", doc_type="receipt")
        spend("b", "0.002000", doc_type="invoice")

        types = {s.key: s.total_usd for s in cost_report(session).by_doc_type}
        assert types == {"receipt": Decimal("0.001000"), "invoice": Decimal("0.002000")}

    def test_latency_percentiles(self, session, spend):
        for i, ms in enumerate([100, 200, 300, 400, 5000]):
            spend(f"d{i}", "0.001", latency=ms)

        report = cost_report(session)
        assert report.latency_p50_ms == 300
        assert report.latency_p95_ms == 5000

    def test_tokens_are_summed(self, session, spend):
        spend("a", "0.001", tokens=700)
        spend("b", "0.001", tokens=300)
        assert cost_report(session).input_tokens == 1000


class TestWindow:
    def test_a_window_excludes_calls_outside_it(self, session, spend):
        old = spend("a", "0.001000")
        old.created_at = utcnow() - timedelta(days=40)
        session.commit()
        spend("b", "0.002000")

        recent = cost_report(session, since=date.today() - timedelta(days=7))
        assert recent.total_usd == Decimal("0.002000")

    def test_the_until_bound_is_inclusive_of_the_whole_day(self, session, spend):
        spend("a", "0.001000")
        assert cost_report(session, until=date.today()).calls == 1


class TestEndpoint:
    def test_it_needs_a_key_like_everything_else(self, anon_client):
        """Protected without any code in the route: the check is on the app."""
        assert anon_client.get("/costs").status_code == 401

    def test_it_reports(self, client, session_factory):
        with session_factory() as s:
            document, _ = repository.ingest(
                s, data=TINY_PNG, mime_type="image/png", original_filename="a.png"
            )
            repository.record_extraction(
                s,
                document_id=document.id,
                doc_type="receipt",
                schema_version=1,
                model_used="m",
                raw_json={},
                cost_usd=Decimal("0.001205"),
            )

        body = client.get("/costs").json()
        assert body["calls"] == 1
        assert body["total_usd"] == "0.001205"

    def test_dates_are_validated(self, client):
        assert client.get("/costs?since=yesterday").status_code == 422


def test_failed_rows_are_what_wasted_counts(session, spend):
    """The link between the two halves of this change."""
    spend.failed("a", "0.003000")
    row = session.query(Extraction).one()
    assert row.raw_json is None
    assert cost_report(session).wasted_usd == Decimal("0.003000")
