"""
The analyst's tools.

These carry the claim the phase exists to make: the model never does
arithmetic, so the arithmetic has to be right here. Every one of these runs
without a model.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from analyst import tools
from core import repository
from core.models import Transaction

from .conftest import TINY_PNG


@pytest.fixture
def ledger(session):
    """A small ledger built the way the pipeline builds one."""
    made: list[Transaction] = []

    def _add(
        amount,
        *,
        when=None,
        who="MERCADONA S.A.",
        currency="EUR",
        tax=None,
        direction="out",
        sha=None,
    ):
        document, _ = repository.ingest(
            session,
            data=TINY_PNG + (sha or str(len(made))).encode(),
            mime_type="image/png",
            original_filename=f"r{len(made)}.png",
        )
        extraction = repository.record_extraction(
            session,
            document_id=document.id,
            doc_type="receipt",
            schema_version=1,
            model_used="fake",
            raw_json={"total": amount},
            validation_errors=[],
        )
        row = repository.record_transaction(
            session,
            extraction=extraction,
            values={
                "source_type": "receipt",
                "direction": direction,
                "amount": Decimal(amount),
                "tax_amount": Decimal(tax) if tax else None,
                "currency": currency,
                "occurred_on": when,
                "counterparty": who,
            },
        )
        made.append(row)
        return row

    return _add


class TestTotalSpend:
    def test_it_sums_what_is_there(self, session, ledger):
        ledger("10.00")
        ledger("5.50")

        totals = tools.total_spend(session)

        assert totals.amounts[0].total == Decimal("15.50")
        assert totals.amounts[0].count == 2

    def test_the_sum_is_exact(self, session, ledger):
        """Three tenths that a float would get wrong."""
        for amount in ("0.10", "0.20", "0.30"):
            ledger(amount)

        total = tools.total_spend(session).amounts[0].total

        assert total == Decimal("0.6000")
        assert isinstance(total, Decimal)

    def test_an_empty_ledger_sums_to_nothing_rather_than_failing(self, session):
        assert tools.total_spend(session).amounts == []

    def test_a_date_window_is_honoured(self, session, ledger):
        ledger("10.00", when=date(2026, 1, 15))
        ledger("99.00", when=date(2026, 6, 15))

        totals = tools.total_spend(
            session, start=date(2026, 1, 1), end=date(2026, 3, 31)
        )

        assert totals.amounts[0].total == Decimal("10.00")

    def test_counterparty_matching_is_forgiving(self, session, ledger):
        """The ledger holds MERCADONA S.A.; a person asks about mercadona."""
        ledger("10.00", who="MERCADONA S.A.")
        ledger("20.00", who="CAFE LUNA")

        totals = tools.total_spend(session, counterparty="mercadona")

        assert totals.amounts[0].total == Decimal("10.00")

    def test_tax_is_summed_separately(self, session, ledger):
        ledger("11.00", tax="1.00")
        ledger("22.00", tax="2.00")

        totals = tools.total_spend(session)

        assert totals.amounts[0].total == Decimal("33.00")
        assert totals.tax[0].total == Decimal("3.00")

    def test_money_in_is_not_counted_as_money_out(self, session, ledger):
        ledger("10.00", direction="out")
        ledger("500.00", direction="in")

        assert tools.total_spend(session).amounts[0].total == Decimal("10.00")


class TestItNeverMixesCurrencies:
    """8.30 EUR plus 8.30 USD is not 16.60 of anything."""

    def test_two_currencies_are_reported_separately(self, session, ledger):
        ledger("10.00", currency="EUR")
        ledger("10.00", currency="USD")

        amounts = tools.total_spend(session).amounts

        assert len(amounts) == 2
        assert {m.currency for m in amounts} == {"EUR", "USD"}
        assert all(m.total == Decimal("10.00") for m in amounts)

    def test_a_mixed_result_says_not_to_add_it_up(self, session, ledger):
        ledger("10.00", currency="EUR")
        ledger("10.00", currency="USD")

        assert "must not be added" in tools.total_spend(session).as_dict()["note"]

    def test_a_single_currency_carries_no_warning(self, session, ledger):
        ledger("10.00", currency="EUR")
        assert tools.total_spend(session).as_dict()["note"] is None

    def test_an_unstated_currency_gets_its_own_bucket(self, session, ledger):
        """Not folded into the majority: the document did not say."""
        ledger("10.00", currency="EUR")
        ledger("7.00", currency=None)

        amounts = {m.currency: m.total for m in tools.total_spend(session).amounts}

        assert amounts[None] == Decimal("7.00")
        assert amounts["EUR"] == Decimal("10.00")

    def test_unstated_is_labelled_rather_than_left_blank(self, session, ledger):
        ledger("7.00", currency=None)
        assert tools.total_spend(session).as_dict()["totals"][0]["currency"] == "unstated"


class TestItOnlyCountsLiveRows:
    def test_a_superseded_transaction_is_not_counted(self, session, ledger):
        """
        The failure this prevents: a re-extracted receipt counted twice. It is
        the reason every query starts from is_current rather than remembering
        to filter.
        """
        row = ledger("10.00")
        extraction = session.get(
            __import__("core.models", fromlist=["Extraction"]).Extraction,
            row.source_extraction_id,
        )
        repository.record_transaction(
            session,
            extraction=extraction,
            values={"source_type": "receipt", "direction": "out", "amount": Decimal("99.00")},
        )

        totals = tools.total_spend(session)

        assert totals.amounts[0].count == 1
        assert totals.amounts[0].total == Decimal("99.00")


class TestCitations:
    def test_every_total_carries_its_sources(self, session, ledger):
        a = ledger("10.00")
        b = ledger("5.00")

        sources = tools.total_spend(session).document_ids

        assert set(sources) == {a.document_id, b.document_id}

    def test_a_source_resolves_to_the_document_and_the_extraction(
        self, session, ledger
    ):
        row = ledger("10.00", who="CAFE LUNA")

        source = tools.get_source(session, row.id)

        assert source["amount"] == "10.00"
        assert source["counterparty"] == "CAFE LUNA"
        assert source["document"]["filename"] == "r0.png"
        assert source["extraction"]["raw_json"] == {"total": "10.00"}
        assert source["extraction"]["model"] == "fake"

    def test_an_unknown_transaction_returns_nothing_rather_than_raising(self, session):
        assert tools.get_source(session, "does-not-exist") is None


class TestListing:
    def test_rows_come_back_newest_first(self, session, ledger):
        ledger("1.00", when=date(2026, 1, 1))
        ledger("2.00", when=date(2026, 6, 1))

        rows = tools.list_transactions(session)

        assert rows[0]["occurred_on"] == "2026-06-01"

    def test_a_minimum_amount_filters(self, session, ledger):
        ledger("1.00")
        ledger("100.00")

        rows = tools.list_transactions(session, min_amount=Decimal("50"))

        assert len(rows) == 1
        assert rows[0]["amount"] == "100.00"

    def test_the_limit_is_capped_so_a_context_window_cannot_be_flooded(
        self, session, ledger
    ):
        for _ in range(5):
            ledger("1.00")

        assert len(tools.list_transactions(session, limit=10_000)) == 5
        assert len(tools.list_transactions(session, limit=2)) == 2

    def test_a_nonsense_limit_still_returns_something(self, session, ledger):
        ledger("1.00")
        assert len(tools.list_transactions(session, limit=0)) == 1

    def test_every_row_names_its_document(self, session, ledger):
        row = ledger("1.00")
        assert tools.list_transactions(session)[0]["document_id"] == row.document_id


class TestGrouping:
    def test_spend_by_counterparty_is_ordered_by_size(self, session, ledger):
        ledger("5.00", who="SMALL SHOP")
        ledger("50.00", who="BIG SHOP")

        groups = tools.spend_by_counterparty(session)

        assert groups[0]["counterparty"] == "BIG SHOP"
        assert groups[0]["total"] == "50.00"

    def test_grouping_also_splits_by_currency(self, session, ledger):
        ledger("10.00", who="SHOP", currency="EUR")
        ledger("10.00", who="SHOP", currency="USD")

        groups = tools.spend_by_counterparty(session)

        assert len(groups) == 2
        assert {g["currency"] for g in groups} == {"EUR", "USD"}

    def test_spend_by_month_is_chronological(self, session, ledger):
        ledger("1.00", when=date(2026, 3, 4))
        ledger("2.00", when=date(2026, 1, 9))

        months = tools.spend_by_month(session)

        assert [m["month"] for m in months] == ["2026-01", "2026-03"]

    def test_several_rows_in_one_month_are_added(self, session, ledger):
        ledger("1.50", when=date(2026, 3, 4))
        ledger("2.50", when=date(2026, 3, 20))

        assert tools.spend_by_month(session)[0]["total"] == "4.00"

    def test_undated_rows_are_reported_rather_than_dropped(self, session, ledger):
        """A receipt whose date did not survive OCR is still money spent."""
        ledger("9.00", when=None)

        months = tools.spend_by_month(session)

        assert months[0]["month"] == "undated"
        assert months[0]["total"] == "9.00"


class TestLedgerSummary:
    def test_an_empty_ledger_reports_zero_rather_than_failing(self, session):
        summary = tools.ledger_summary(session)

        assert summary["transactions"] == 0
        assert summary["earliest"] is None
        assert summary["currencies"] == []

    def test_it_reports_the_range_and_currencies_present(self, session, ledger):
        ledger("1.00", when=date(2026, 1, 5), currency="EUR")
        ledger("2.00", when=date(2026, 9, 9), currency="USD")

        summary = tools.ledger_summary(session)

        assert summary["transactions"] == 2
        assert summary["earliest"] == "2026-01-05"
        assert summary["latest"] == "2026-09-09"
        assert summary["currencies"] == ["EUR", "USD"]

    def test_it_counts_money_in_as_well_as_out(self, session, ledger):
        """Orientation, not a spend query: both directions are in the ledger."""
        ledger("1.00", direction="out")
        ledger("2.00", direction="in")

        assert tools.ledger_summary(session)["transactions"] == 2
