"""
The last deterministic step: a validated extraction becoming a ledger row.

Two things are under test. That the mapping is declared correctly -- caught
at definition time, not on some later document -- and that nothing is
invented on the way into a table the analyst agent will treat as fact.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from pydantic import ValidationError

from core import repository
from core.models import Transaction
from doctypes.canonical import to_ledger
from doctypes.definition import Definition

from .conftest import TINY_PNG

FIELDS = {
    "vendor": {"type": "string", "description": "The vendor."},
    "tax_id": {"type": "string", "description": "The tax id."},
    "issued_on": {"type": "date", "description": "The date."},
    "total": {"type": "money", "description": "The total.", "required": True},
    "tax_amount": {"type": "money", "description": "The tax."},
    "currency": {"type": "string", "description": "The currency."},
}

FULL_MAP = {
    "direction": "out",
    "amount": "total",
    "tax_amount": "tax_amount",
    "occurred_on": "issued_on",
    "counterparty": "vendor",
    "counterparty_tax_id": "tax_id",
    "currency": "currency",
}


def _definition(canonical=FULL_MAP, **overrides) -> Definition:
    spec = {
        "name": "receipt",
        "matches": {"keywords": ["total"]},
        "fields": FIELDS,
        "canonical": canonical,
    }
    spec.update(overrides)
    return Definition.model_validate(spec)


CLEAN = {
    "vendor": "CAFE LUNA",
    "tax_id": "A46103834",
    "issued_on": date(2026, 5, 12),
    "total": Decimal("8.30"),
    "tax_amount": Decimal("0.75"),
    "currency": "EUR",
}


class TestDeclaringTheMapping:
    """Caught at authoring time, because the ledger columns are typed."""

    def test_a_valid_mapping_is_accepted(self):
        assert _definition().canonical.amount == "total"

    def test_a_mapping_to_an_unknown_field_is_refused(self):
        with pytest.raises(ValidationError, match="unknown field"):
            _definition({"direction": "out", "amount": "nonexistent"})

    def test_amount_must_point_at_a_money_field(self):
        with pytest.raises(ValidationError, match="must point at a money field"):
            _definition({"direction": "out", "amount": "vendor"})

    def test_occurred_on_must_point_at_a_date_field(self):
        with pytest.raises(ValidationError, match="must point at a date field"):
            _definition(
                {"direction": "out", "amount": "total", "occurred_on": "vendor"}
            )

    def test_tax_amount_must_point_at_a_money_field(self):
        with pytest.raises(ValidationError, match="must point at a money field"):
            _definition(
                {"direction": "out", "amount": "total", "tax_amount": "vendor"}
            )

    def test_an_unknown_direction_is_refused(self):
        with pytest.raises(ValidationError):
            _definition({"direction": "sideways", "amount": "total"})

    def test_amount_is_required(self):
        """A ledger row without an amount is not a transaction."""
        with pytest.raises(ValidationError):
            _definition({"direction": "out"})

    def test_a_type_may_have_no_mapping_at_all(self):
        assert _definition(canonical=None).canonical is None


class TestBuildingTheRow:
    def test_a_clean_extraction_maps_completely(self):
        values, reason = to_ledger(_definition(), CLEAN)

        assert reason is None
        assert values == {
            "source_type": "receipt",
            "direction": "out",
            "amount": Decimal("8.30"),
            "tax_amount": Decimal("0.75"),
            "occurred_on": date(2026, 5, 12),
            "counterparty": "CAFE LUNA",
            "counterparty_tax_id": "A46103834",
            "currency": "EUR",
        }

    def test_amounts_stay_decimal(self):
        values, _ = to_ledger(_definition(), CLEAN)
        assert isinstance(values["amount"], Decimal)
        assert isinstance(values["tax_amount"], Decimal)

    def test_a_type_with_no_mapping_stops_short_of_the_ledger(self):
        values, reason = to_ledger(_definition(canonical=None), CLEAN)
        assert values is None
        assert "no canonical mapping" in reason

    def test_a_null_amount_produces_no_row(self):
        values, reason = to_ledger(_definition(), CLEAN | {"total": None})
        assert values is None
        assert "no amount to record" in reason

    def test_only_the_mapped_fields_are_carried(self):
        """The ledger is narrow on purpose; the rest stays in raw_json."""
        minimal = _definition({"direction": "out", "amount": "total"})
        values, _ = to_ledger(minimal, CLEAN)
        assert set(values) == {"source_type", "direction", "amount"}

    def test_missing_optional_values_become_null_not_guesses(self):
        sparse = {"total": Decimal("4.20")}
        values, _ = to_ledger(_definition(), sparse)
        assert values["counterparty"] is None
        assert values["currency"] is None
        assert values["occurred_on"] is None
        assert values["tax_amount"] is None


class TestItNeverInvents:
    def test_a_currency_that_is_not_an_iso_code_is_dropped(self):
        """Writing "Euros" into a 3-char ledger column helps nobody."""
        values, _ = to_ledger(_definition(), CLEAN | {"currency": "Euros"})
        assert values["currency"] is None

    def test_a_lowercase_code_is_normalised_not_rejected(self):
        values, _ = to_ledger(_definition(), CLEAN | {"currency": "eur"})
        assert values["currency"] == "EUR"

    def test_a_missing_currency_stays_missing(self):
        """A Spanish receipt is usually EUR, and usually has no place here."""
        values, _ = to_ledger(_definition(), CLEAN | {"currency": None})
        assert values["currency"] is None

    def test_a_date_of_the_wrong_type_is_dropped_rather_than_coerced(self):
        values, _ = to_ledger(_definition(), CLEAN | {"issued_on": "12/05/2026"})
        assert values["occurred_on"] is None

    def test_a_non_decimal_amount_produces_no_row(self):
        values, reason = to_ledger(_definition(), CLEAN | {"total": "8.30"})
        assert values is None
        assert "not an amount" in reason

    def test_counterparty_whitespace_is_collapsed(self):
        values, _ = to_ledger(_definition(), CLEAN | {"vendor": "  CAFE   LUNA \n"})
        assert values["counterparty"] == "CAFE LUNA"

    def test_an_overlong_counterparty_is_truncated_to_the_column(self):
        values, _ = to_ledger(_definition(), CLEAN | {"vendor": "X" * 500})
        assert len(values["counterparty"]) == 200

    def test_an_empty_counterparty_becomes_null(self):
        values, _ = to_ledger(_definition(), CLEAN | {"vendor": "   "})
        assert values["counterparty"] is None


class TestRecordingIt:
    @pytest.fixture
    def extraction(self, session):
        document, _ = repository.ingest(
            session, data=TINY_PNG, mime_type="image/png", original_filename="a.png"
        )
        return repository.record_extraction(
            session,
            document_id=document.id,
            doc_type="receipt",
            schema_version=1,
            model_used="fake",
            raw_json={"total": "8,30"},
            validation_errors=[],
        )

    def _values(self, amount="8.30"):
        return {
            "source_type": "receipt",
            "direction": "out",
            "amount": Decimal(amount),
        }

    def test_it_writes_a_row_traceable_to_its_extraction(self, session, extraction):
        transaction = repository.record_transaction(
            session, extraction=extraction, values=self._values()
        )

        assert transaction.source_extraction_id == extraction.id
        assert transaction.document_id == extraction.document_id
        assert transaction.is_current is True
        assert transaction.user_verified is False

    def test_a_second_row_supersedes_the_first(self, session, extraction):
        """A re-run receipt must not be counted twice."""
        repository.record_transaction(
            session, extraction=extraction, values=self._values("8.30")
        )
        repository.record_transaction(
            session, extraction=extraction, values=self._values("9.99")
        )

        rows = session.query(Transaction).all()
        assert len(rows) == 2
        assert [r.is_current for r in rows] == [False, True]

    def test_current_transaction_returns_the_live_one(self, session, extraction):
        repository.record_transaction(
            session, extraction=extraction, values=self._values("8.30")
        )
        second = repository.record_transaction(
            session, extraction=extraction, values=self._values("9.99")
        )

        live = repository.current_transaction(session, extraction.document_id)
        assert live.id == second.id
        assert live.amount == Decimal("9.9900")

    def test_summing_the_live_rows_does_not_double_count(self, session, extraction):
        for amount in ("8.30", "9.99", "1.00"):
            repository.record_transaction(
                session, extraction=extraction, values=self._values(amount)
            )

        live = session.query(Transaction).filter_by(is_current=True).all()
        assert sum(r.amount for r in live) == Decimal("1.0000")

    def test_a_float_amount_is_refused(self, session, extraction):
        with pytest.raises(TypeError, match="Decimal"):
            repository.record_transaction(
                session,
                extraction=extraction,
                values={"source_type": "r", "direction": "out", "amount": 8.30},
            )

    def test_a_document_with_no_row_returns_none(self, session, extraction):
        assert repository.current_transaction(session, extraction.document_id) is None


class TestTaxLines:
    """
    An invoice can print GST and VAT separately with no combined line. The
    model records each as printed; the ledger's tax is their sum, added here
    in code, because the model is told never to add anything up.
    """

    @staticmethod
    def _invoice():
        from doctypes import seed

        return next(d for d in seed.load_seeds() if d.name == "invoice")

    def _ledger(self, **data):
        from doctypes.validate import coerce

        coerced, _ = coerce(self._invoice(), {"total": "491.67", **data})
        return to_ledger(self._invoice(), coerced)[0]

    def test_separate_taxes_are_summed_in_code(self):
        values = self._ledger(taxes=[
            {"name": "GST", "rate": "18%", "amount": "89.03"},
            {"name": "VAT", "rate": "3.39%", "amount": "16.76"},
        ])
        assert values["tax_amount"] == Decimal("105.79")

    def test_a_printed_total_tax_wins_over_the_list(self):
        values = self._ledger(tax_amount="100.00", taxes=[{"name": "VAT", "amount": "16.76"}])
        assert values["tax_amount"] == Decimal("100.00")

    def test_one_unreadable_line_makes_the_total_unknown_not_smaller(self):
        values = self._ledger(taxes=[
            {"name": "GST", "amount": "89.03"},
            {"name": "VAT", "amount": "n/a"},
        ])
        assert values["tax_amount"] is None

    def test_no_taxes_means_no_tax(self):
        assert self._ledger(taxes=None)["tax_amount"] is None

    def test_tax_lines_must_point_at_a_list_with_amounts(self):
        with pytest.raises(ValidationError, match="must point at a list"):
            _definition({"direction": "out", "amount": "total", "tax_lines": "vendor"})


class TestSeedingDoesNotChurnVersions:
    def test_a_new_optional_format_field_is_not_a_content_change(self, session):
        """
        Adding canonical.tax_lines to the format once re-versioned the receipt
        type although receipt.yaml had not changed: stored JSON lacked the new
        default. Comparing through the model fills it in on both sides.
        """
        from doctypes import seed

        definitions = seed.load_seeds()
        seed.sync(session, definitions)
        row = repository.get_active_document_type(session, "receipt")
        stale = dict(row.definition)
        stale["canonical"] = {k: v for k, v in stale["canonical"].items() if k != "tax_lines"}
        row.definition = stale
        session.commit()

        assert seed.sync(session, definitions)["receipt"].startswith("unchanged")
