"""
Coercion and the seven deterministic rules.

Two things get the most attention here. Money is never allowed to become a
float, and the comma decimal separator is load-bearing -- a live extraction
returns "7,15" because that is what the receipt prints. And every rule is
exercised against a null value, because a model that found nothing answers with
null and a validator that raises on it takes the whole pipeline down.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from doctypes.definition import (
    Definition,
    FieldSpec,
    FieldType,
    LineItemsSumRule,
    Matchers,
    NotFutureRule,
    PositiveRule,
    RangeRule,
    RegexRule,
    RequiredRule,
    SumEqualsRule,
)
from doctypes.validate import coerce, evaluate, validate

TODAY = datetime.now(UTC).date()  # the clock the rules themselves read


def _spec(type_=FieldType.STRING, **kwargs) -> FieldSpec:
    return FieldSpec(type=type_, description="the field under test", **kwargs)


def _definition(fields: dict, rules=()) -> Definition:
    return Definition(
        name="receipt",
        matches=Matchers(keywords=["receipt"]),
        fields=fields,
        rules=list(rules),
    )


def _coerce_one(type_, value, **kwargs):
    """Coerce a single value and return (coerced, errors) for it alone."""
    definition = _definition({"f": _spec(type_, **kwargs)})
    data, errors = coerce(definition, {"f": value})
    return data["f"], errors


def _check(rule, fields: dict, data: dict) -> list[str]:
    """Evaluate one rule against one already-coerced row."""
    return evaluate(_definition(fields, [rule]), data)


# --- Coercion ---------------------------------------------------------------


class TestMoneyCoercion:
    def test_plain_decimal_string(self):
        value, errors = _coerce_one(FieldType.MONEY, "1234.56")
        assert value == Decimal("1234.56")
        assert errors == []

    def test_comma_is_a_decimal_separator(self):
        """Spanish receipts print "7,15" and the prompt asks for it as printed."""
        value, errors = _coerce_one(FieldType.MONEY, "7,15")
        assert value == Decimal("7.15")
        assert errors == []

    def test_result_is_decimal_not_float(self):
        value, _ = _coerce_one(FieldType.MONEY, "0,10")
        assert isinstance(value, Decimal)
        assert not isinstance(value, float)

    def test_three_tenths_the_way_floats_cannot(self):
        """The reason money is Decimal at all: 0.1 + 0.2 == 0.3 must hold."""
        a, _ = _coerce_one(FieldType.MONEY, "0.10")
        b, _ = _coerce_one(FieldType.MONEY, "0.20")
        assert a + b == Decimal("0.30")

    def test_negative(self):
        value, errors = _coerce_one(FieldType.MONEY, "-42,00")
        assert value == Decimal("-42.00")
        assert errors == []

    def test_whole_number_without_decimals(self):
        value, _ = _coerce_one(FieldType.MONEY, "12")
        assert value == Decimal("12")

    def test_single_decimal_place(self):
        value, _ = _coerce_one(FieldType.MONEY, "12,5")
        assert value == Decimal("12.5")

    def test_thousands_separator_is_rejected_rather_than_misread(self):
        """
        The dangerous case. "1,234" normalised naively is 1.234 -- three orders
        of magnitude gone, with no error. The definition format has no
        thousands separators, so this can only be a value we do not understand.
        """
        value, errors = _coerce_one(FieldType.MONEY, "1,234")
        assert value is None
        assert len(errors) == 1
        assert "f:" in errors[0]

    def test_mixed_separators_are_rejected(self):
        value, errors = _coerce_one(FieldType.MONEY, "1.234,56")
        assert value is None
        assert errors

    def test_currency_symbol_is_rejected(self):
        value, errors = _coerce_one(FieldType.MONEY, "7,15 EUR")
        assert value is None
        assert errors

    def test_three_decimal_places_are_rejected(self):
        value, errors = _coerce_one(FieldType.MONEY, "7.153")
        assert value is None
        assert errors

    def test_surrounding_whitespace_is_tolerated(self):
        value, errors = _coerce_one(FieldType.MONEY, "  7,15 ")
        assert value == Decimal("7.15")
        assert errors == []

    def test_a_bare_number_is_accepted(self):
        """The schema asks for a string, but a number is unambiguous."""
        value, errors = _coerce_one(FieldType.MONEY, 7.15)
        assert value == Decimal("7.15")
        assert errors == []

    def test_an_integer_is_accepted(self):
        value, _ = _coerce_one(FieldType.MONEY, 12)
        assert value == Decimal("12")

    def test_boolean_is_not_an_amount(self):
        """bool is an int in Python; a yes/no answer is not a total."""
        value, errors = _coerce_one(FieldType.MONEY, True)
        assert value is None
        assert errors

    def test_garbage_reports_instead_of_raising(self):
        value, errors = _coerce_one(FieldType.MONEY, "n/a")
        assert value is None
        assert "n/a" in errors[0]

    def test_none_stays_none(self):
        value, errors = _coerce_one(FieldType.MONEY, None)
        assert value is None
        assert errors == []


class TestDateCoercion:
    def test_iso_date(self):
        value, errors = _coerce_one(FieldType.DATE, "2026-05-14")
        assert value == date(2026, 5, 14)
        assert errors == []

    def test_result_is_a_date_not_a_string(self):
        value, _ = _coerce_one(FieldType.DATE, "2026-05-14")
        assert isinstance(value, date)

    def test_european_format_is_rejected(self):
        value, errors = _coerce_one(FieldType.DATE, "14/05/2026")
        assert value is None
        assert "YYYY-MM-DD" in errors[0]

    def test_compact_iso_is_rejected(self):
        """fromisoformat accepts "20260514"; nothing in this system emits it."""
        value, errors = _coerce_one(FieldType.DATE, "20260514")
        assert value is None
        assert errors

    def test_impossible_date_is_rejected(self):
        value, errors = _coerce_one(FieldType.DATE, "2026-02-30")
        assert value is None
        assert "not a real date" in errors[0]

    def test_wrong_type_is_rejected(self):
        value, errors = _coerce_one(FieldType.DATE, 20260514)
        assert value is None
        assert errors

    def test_none_stays_none(self):
        value, errors = _coerce_one(FieldType.DATE, None)
        assert value is None
        assert errors == []


class TestScalarCoercion:
    def test_integer(self):
        assert _coerce_one(FieldType.INTEGER, 3) == (3, [])

    def test_integer_rejects_a_string(self):
        value, errors = _coerce_one(FieldType.INTEGER, "3")
        assert value is None
        assert errors

    def test_integer_rejects_a_boolean(self):
        value, errors = _coerce_one(FieldType.INTEGER, True)
        assert value is None
        assert errors

    def test_number(self):
        value, errors = _coerce_one(FieldType.NUMBER, 0.21)
        assert value == pytest.approx(0.21)
        assert errors == []

    def test_number_accepts_an_integer(self):
        value, _ = _coerce_one(FieldType.NUMBER, 21)
        assert value == 21.0
        assert isinstance(value, float)

    def test_number_rejects_a_string(self):
        value, errors = _coerce_one(FieldType.NUMBER, "0.21")
        assert value is None
        assert errors

    def test_boolean(self):
        assert _coerce_one(FieldType.BOOLEAN, False) == (False, [])

    def test_boolean_rejects_a_string(self):
        value, errors = _coerce_one(FieldType.BOOLEAN, "true")
        assert value is None
        assert errors

    def test_string(self):
        assert _coerce_one(FieldType.STRING, "Cafe Central") == ("Cafe Central", [])

    def test_string_rejects_a_number(self):
        value, errors = _coerce_one(FieldType.STRING, 42)
        assert value is None
        assert errors

    def test_empty_string_survives(self):
        """Coercion types values; whether "" counts as missing is a rule's call."""
        assert _coerce_one(FieldType.STRING, "") == ("", [])


class TestListCoercion:
    definition = _definition(
        {
            "line_items": _spec(
                FieldType.LIST,
                item_fields={
                    "description": _spec(FieldType.STRING),
                    "qty": _spec(FieldType.INTEGER),
                    "amount": _spec(FieldType.MONEY),
                },
            )
        }
    )

    def test_entries_are_coerced_recursively(self):
        data, errors = coerce(
            self.definition,
            {"line_items": [{"description": "Cafe", "qty": 2, "amount": "3,60"}]},
        )
        assert errors == []
        assert data["line_items"] == [
            {"description": "Cafe", "qty": 2, "amount": Decimal("3.60")}
        ]

    def test_every_entry_is_coerced(self):
        data, errors = coerce(
            self.definition,
            {
                "line_items": [
                    {"description": "Cafe", "qty": 2, "amount": "3,60"},
                    {"description": "Tostada", "qty": 1, "amount": "1,80"},
                ]
            },
        )
        assert errors == []
        assert [e["amount"] for e in data["line_items"]] == [Decimal("3.60"), Decimal("1.80")]

    def test_an_absent_item_field_is_null_not_an_error(self):
        """
        build_json_schema does not nullable-wrap inside `items`, so an optional
        item field may simply not come back.
        """
        data, errors = coerce(self.definition, {"line_items": [{"amount": "3,60"}]})
        assert errors == []
        assert data["line_items"][0] == {
            "description": None,
            "qty": None,
            "amount": Decimal("3.60"),
        }

    def test_an_error_names_the_entry_and_the_field(self):
        _, errors = coerce(
            self.definition,
            {"line_items": [{"amount": "3,60"}, {"amount": "nope"}]},
        )
        assert len(errors) == 1
        assert errors[0].startswith("line_items[1].amount:")

    def test_a_bad_entry_does_not_hide_the_good_ones(self):
        data, errors = coerce(
            self.definition,
            {"line_items": [{"amount": "3,60"}, {"amount": "nope"}]},
        )
        assert data["line_items"][0]["amount"] == Decimal("3.60")
        assert data["line_items"][1]["amount"] is None
        assert errors

    def test_a_non_list_is_reported(self):
        data, errors = coerce(self.definition, {"line_items": {"amount": "3,60"}})
        assert data["line_items"] is None
        assert "expected a list" in errors[0]

    def test_a_non_object_entry_keeps_its_position(self):
        """A later line_items_sum error names an index; it has to be the real one."""
        data, errors = coerce(self.definition, {"line_items": ["Cafe 3,60", {"amount": "1,80"}]})
        assert errors[0].startswith("line_items[0]:")
        assert data["line_items"][1]["amount"] == Decimal("1.80")

    def test_none_stays_none(self):
        data, errors = coerce(self.definition, {"line_items": None})
        assert data["line_items"] is None
        assert errors == []

    def test_empty_list_is_fine(self):
        data, errors = coerce(self.definition, {"line_items": []})
        assert data["line_items"] == []
        assert errors == []

    def test_nested_lists_recurse(self):
        definition = _definition(
            {
                "sections": _spec(
                    FieldType.LIST,
                    item_fields={
                        "rows": _spec(
                            FieldType.LIST,
                            item_fields={"amount": _spec(FieldType.MONEY)},
                        )
                    },
                )
            }
        )
        data, errors = coerce(definition, {"sections": [{"rows": [{"amount": "9,99"}]}]})
        assert errors == []
        assert data["sections"][0]["rows"][0]["amount"] == Decimal("9.99")


class TestCoerceShape:
    definition = _definition({"total": _spec(FieldType.MONEY), "vendor": _spec()})

    def test_a_missing_key_becomes_null(self):
        """Downstream indexes by the definition's names and should not have to guard."""
        data, errors = coerce(self.definition, {"total": "7,15"})
        assert data == {"total": Decimal("7.15"), "vendor": None}
        assert errors == []

    def test_undeclared_keys_are_dropped(self):
        data, _ = coerce(self.definition, {"total": "7,15", "vendor": "Cafe", "tip": "1,00"})
        assert set(data) == {"total", "vendor"}

    def test_a_non_object_payload_is_reported_not_raised(self):
        data, errors = coerce(self.definition, ["7,15"])
        assert data == {"total": None, "vendor": None}
        assert "expected a JSON object" in errors[0]

    def test_errors_accumulate_across_fields(self):
        """One malformed field must not hide what the others say."""
        _, errors = coerce(self.definition, {"total": "n/a", "vendor": 42})
        assert len(errors) == 2


# --- Rules ------------------------------------------------------------------


class TestRequiredRule:
    fields = {"vendor": _spec(), "total": _spec(FieldType.MONEY), "items": _spec(
        FieldType.LIST, item_fields={"amount": _spec(FieldType.MONEY)}
    )}
    rule = RequiredRule(field="vendor")

    def test_passes_when_present(self):
        assert _check(self.rule, self.fields, {"vendor": "Cafe Central"}) == []

    def test_fails_when_null(self):
        assert _check(self.rule, self.fields, {"vendor": None}) == [
            "vendor is required but was not extracted"
        ]

    def test_fails_when_the_key_is_absent(self):
        assert len(_check(self.rule, self.fields, {})) == 1

    def test_fails_on_an_empty_string(self):
        """A model that found nothing sometimes answers "" rather than null."""
        assert len(_check(self.rule, self.fields, {"vendor": ""})) == 1

    def test_fails_on_whitespace_only(self):
        assert len(_check(self.rule, self.fields, {"vendor": "   "})) == 1

    def test_fails_on_an_empty_list(self):
        rule = RequiredRule(field="items")
        assert len(_check(rule, self.fields, {"items": []})) == 1

    def test_zero_is_an_answer_not_an_absence(self):
        rule = RequiredRule(field="total")
        assert _check(rule, self.fields, {"total": Decimal("0")}) == []

    def test_custom_message_wins(self):
        rule = RequiredRule(field="vendor", message="the till roll always prints a vendor")
        assert _check(rule, self.fields, {"vendor": None}) == [
            "the till roll always prints a vendor"
        ]


class TestPositiveRule:
    fields = {"total": _spec(FieldType.MONEY)}
    rule = PositiveRule(field="total")

    def test_passes_on_a_positive_amount(self):
        assert _check(self.rule, self.fields, {"total": Decimal("7.15")}) == []

    def test_fails_on_zero(self):
        assert len(_check(self.rule, self.fields, {"total": Decimal("0.00")})) == 1

    def test_fails_on_a_negative_amount(self):
        errors = _check(self.rule, self.fields, {"total": Decimal("-7.15")})
        assert errors == ["total must be greater than 0, got -7.15"]

    def test_a_tiny_positive_amount_passes(self):
        assert _check(self.rule, self.fields, {"total": Decimal("0.01")}) == []

    def test_reports_a_null_instead_of_crashing(self):
        errors = _check(self.rule, self.fields, {"total": None})
        assert len(errors) == 1
        assert "null" in errors[0]

    def test_reports_a_non_number_instead_of_crashing(self):
        """Coercion leaves None behind, but a hand-built row might not."""
        errors = _check(self.rule, self.fields, {"total": "7,15"})
        assert len(errors) == 1
        assert "not a number" in errors[0]

    def test_custom_message_wins(self):
        rule = PositiveRule(field="total", message="a refund is a different document type")
        assert _check(rule, self.fields, {"total": Decimal("-1")}) == [
            "a refund is a different document type"
        ]


class TestRangeRule:
    fields = {"tax_rate": _spec(FieldType.NUMBER)}

    def test_passes_inside_the_range(self):
        rule = RangeRule(field="tax_rate", min=0.0, max=0.3)
        assert _check(rule, self.fields, {"tax_rate": 0.21}) == []

    def test_the_bounds_are_inclusive(self):
        rule = RangeRule(field="tax_rate", min=0.0, max=0.3)
        assert _check(rule, self.fields, {"tax_rate": 0.0}) == []
        assert _check(rule, self.fields, {"tax_rate": 0.3}) == []

    def test_fails_below_min(self):
        rule = RangeRule(field="tax_rate", min=0.0, max=0.3)
        errors = _check(rule, self.fields, {"tax_rate": -0.01})
        assert len(errors) == 1
        assert "at least" in errors[0]

    def test_fails_above_max(self):
        rule = RangeRule(field="tax_rate", min=0.0, max=0.3)
        errors = _check(rule, self.fields, {"tax_rate": 0.31})
        assert len(errors) == 1
        assert "at most" in errors[0]

    def test_only_a_minimum(self):
        rule = RangeRule(field="tax_rate", min=0.0)
        assert _check(rule, self.fields, {"tax_rate": 99.0}) == []
        assert len(_check(rule, self.fields, {"tax_rate": -1.0})) == 1

    def test_only_a_maximum(self):
        rule = RangeRule(field="tax_rate", max=0.3)
        assert _check(rule, self.fields, {"tax_rate": -99.0}) == []
        assert len(_check(rule, self.fields, {"tax_rate": 0.4})) == 1

    def test_no_bounds_checks_nothing(self):
        """Not even the null, because there is no question being asked."""
        rule = RangeRule(field="tax_rate")
        assert _check(rule, self.fields, {"tax_rate": None}) == []

    def test_reports_a_null_instead_of_crashing(self):
        rule = RangeRule(field="tax_rate", min=0.0)
        errors = _check(rule, self.fields, {"tax_rate": None})
        assert len(errors) == 1
        assert "null" in errors[0]

    def test_compares_in_decimal(self):
        """0.1 + 0.2 > 0.3 as floats; the boundary must not be decided that way."""
        fields = {"amount": _spec(FieldType.MONEY)}
        rule = RangeRule(field="amount", max=0.3)
        assert _check(rule, fields, {"amount": Decimal("0.1") + Decimal("0.2")}) == []

    def test_custom_message_wins(self):
        rule = RangeRule(field="tax_rate", max=0.3, message="Spanish VAT tops out at 21%")
        assert _check(rule, self.fields, {"tax_rate": 0.9}) == ["Spanish VAT tops out at 21%"]


class TestSumEqualsRule:
    fields = {
        "total": _spec(FieldType.MONEY),
        "subtotal": _spec(FieldType.MONEY),
        "tax": _spec(FieldType.MONEY),
    }
    rule = SumEqualsRule(target="total", of=["subtotal", "tax"])

    def _row(self, total, subtotal="10.00", tax="2.10"):
        return {
            "total": Decimal(total),
            "subtotal": Decimal(subtotal),
            "tax": Decimal(tax),
        }

    def test_passes_when_exact(self):
        assert _check(self.rule, self.fields, self._row("12.10")) == []

    def test_passes_at_the_tolerance_boundary(self):
        """Exactly 0.02 out is inside the default tolerance, by <=."""
        assert _check(self.rule, self.fields, self._row("12.12")) == []
        assert _check(self.rule, self.fields, self._row("12.08")) == []

    def test_fails_just_outside_the_tolerance(self):
        assert len(_check(self.rule, self.fields, self._row("12.13"))) == 1
        assert len(_check(self.rule, self.fields, self._row("12.07"))) == 1

    def test_the_error_names_both_sides(self):
        errors = _check(self.rule, self.fields, self._row("99.00"))
        assert "subtotal + tax" in errors[0]
        assert "total" in errors[0]

    def test_an_explicit_tolerance_is_honoured(self):
        rule = SumEqualsRule(target="total", of=["subtotal", "tax"], tolerance=0.0)
        assert _check(rule, self.fields, self._row("12.10")) == []
        assert len(_check(rule, self.fields, self._row("12.11"))) == 1

    def test_a_single_addend(self):
        rule = SumEqualsRule(target="total", of=["subtotal"])
        assert _check(rule, self.fields, self._row("10.00")) == []

    def test_reports_a_null_addend_instead_of_crashing(self):
        row = self._row("12.10") | {"tax": None}
        errors = _check(self.rule, self.fields, row)
        assert len(errors) == 1
        assert "tax is null" in errors[0]

    def test_reports_a_null_target(self):
        row = self._row("12.10") | {"total": None}
        errors = _check(self.rule, self.fields, row)
        assert "total is null" in errors[0]

    def test_reports_every_missing_field_at_once(self):
        errors = _check(self.rule, self.fields, {"total": None, "subtotal": None, "tax": None})
        assert len(errors) == 1
        assert errors[0].count("is null") == 3

    def test_custom_message_wins(self):
        rule = SumEqualsRule(
            target="total", of=["subtotal", "tax"], message="subtotal and tax must make the total"
        )
        assert _check(rule, self.fields, self._row("99.00")) == [
            "subtotal and tax must make the total"
        ]


class TestLineItemsSumRule:
    fields = {
        "total": _spec(FieldType.MONEY),
        "line_items": _spec(
            FieldType.LIST,
            item_fields={"amount": _spec(FieldType.MONEY), "qty": _spec(FieldType.INTEGER)},
        ),
    }
    rule = LineItemsSumRule(list_field="line_items", item_field="amount", target="total")

    def _row(self, total, amounts=("3.60", "1.80", "1.75")):
        return {
            "total": Decimal(total),
            "line_items": [{"amount": Decimal(a), "qty": 1} for a in amounts],
        }

    def test_passes_when_the_lines_add_up(self):
        assert _check(self.rule, self.fields, self._row("7.15")) == []

    def test_fails_when_they_do_not(self):
        errors = _check(self.rule, self.fields, self._row("9.00"))
        assert len(errors) == 1
        assert "7.15" in errors[0]

    def test_passes_at_the_tolerance_boundary(self):
        assert _check(self.rule, self.fields, self._row("7.17")) == []

    def test_fails_just_outside_the_tolerance(self):
        assert len(_check(self.rule, self.fields, self._row("7.18"))) == 1

    def test_an_empty_list_sums_to_zero(self):
        row = {"total": Decimal("0.00"), "line_items": []}
        assert _check(self.rule, self.fields, row) == []

    def test_an_empty_list_against_a_real_total_fails(self):
        row = {"total": Decimal("7.15"), "line_items": []}
        assert len(_check(self.rule, self.fields, row)) == 1

    def test_a_null_line_amount_is_named_not_summed_as_zero(self):
        """An unreadable line makes the total unknown, not smaller."""
        row = self._row("7.15")
        row["line_items"][1]["amount"] = None
        errors = _check(self.rule, self.fields, row)
        assert len(errors) == 1
        assert "line_items[1].amount is null" in errors[0]

    def test_reports_a_null_list(self):
        row = {"total": Decimal("7.15"), "line_items": None}
        errors = _check(self.rule, self.fields, row)
        assert "line_items is null" in errors[0]

    def test_reports_a_null_target(self):
        row = self._row("7.15") | {"total": None}
        errors = _check(self.rule, self.fields, row)
        assert "total is null" in errors[0]

    def test_sums_in_decimal(self):
        """Three cent values a float sum would land 0.0000000000000004 away from."""
        row = {
            "total": Decimal("0.30"),
            "line_items": [{"amount": Decimal("0.10")}, {"amount": Decimal("0.20")}],
        }
        rule = LineItemsSumRule(
            list_field="line_items", item_field="amount", target="total", tolerance=0.0
        )
        assert _check(rule, self.fields, row) == []

    def test_custom_message_wins(self):
        rule = LineItemsSumRule(
            list_field="line_items",
            item_field="amount",
            target="total",
            message="the lines do not add up to the printed total",
        )
        assert _check(rule, self.fields, self._row("9.00")) == [
            "the lines do not add up to the printed total"
        ]


class TestNotFutureRule:
    fields = {"issued_on": _spec(FieldType.DATE)}
    rule = NotFutureRule(field="issued_on")

    def test_today_passes(self):
        assert _check(self.rule, self.fields, {"issued_on": TODAY}) == []

    def test_the_past_passes(self):
        assert _check(self.rule, self.fields, {"issued_on": TODAY - timedelta(days=365)}) == []

    def test_tomorrow_passes_on_the_default_grace(self):
        """grace_days defaults to 1, for timezone skew and till clock drift."""
        assert _check(self.rule, self.fields, {"issued_on": TODAY + timedelta(days=1)}) == []

    def test_the_day_after_tomorrow_fails(self):
        errors = _check(self.rule, self.fields, {"issued_on": TODAY + timedelta(days=2)})
        assert len(errors) == 1
        assert "future" in errors[0] or "after today" in errors[0]

    def test_no_grace_fails_on_tomorrow(self):
        rule = NotFutureRule(field="issued_on", grace_days=0)
        assert len(_check(rule, self.fields, {"issued_on": TODAY + timedelta(days=1)})) == 1

    def test_no_grace_still_passes_today(self):
        rule = NotFutureRule(field="issued_on", grace_days=0)
        assert _check(rule, self.fields, {"issued_on": TODAY}) == []

    def test_a_wide_grace_accepts_a_future_date(self):
        rule = NotFutureRule(field="issued_on", grace_days=30)
        assert _check(rule, self.fields, {"issued_on": TODAY + timedelta(days=29)}) == []

    def test_reports_a_null_instead_of_crashing(self):
        errors = _check(self.rule, self.fields, {"issued_on": None})
        assert len(errors) == 1
        assert "null" in errors[0]

    def test_reports_an_uncoerced_string_instead_of_crashing(self):
        errors = _check(self.rule, self.fields, {"issued_on": "2026-05-14"})
        assert len(errors) == 1
        assert "not a date" in errors[0]

    def test_custom_message_wins(self):
        rule = NotFutureRule(field="issued_on", message="a receipt cannot be dated ahead")
        row = {"issued_on": TODAY + timedelta(days=10)}
        assert _check(rule, self.fields, row) == ["a receipt cannot be dated ahead"]


class TestRegexRule:
    fields = {"tax_id": _spec()}
    rule = RegexRule(field="tax_id", pattern=r"^[A-Z]\d{8}$")

    def test_passes_on_a_match(self):
        assert _check(self.rule, self.fields, {"tax_id": "B12345678"}) == []

    def test_fails_on_a_mismatch(self):
        errors = _check(self.rule, self.fields, {"tax_id": "12345678"})
        assert len(errors) == 1
        assert "does not match" in errors[0]

    def test_an_unanchored_pattern_searches(self):
        """search, not fullmatch, so a half-anchored pattern means what it says."""
        rule = RegexRule(field="tax_id", pattern=r"\d{8}")
        assert _check(rule, self.fields, {"tax_id": "NIF: B12345678 "}) == []

    def test_reports_a_null_instead_of_crashing(self):
        errors = _check(self.rule, self.fields, {"tax_id": None})
        assert len(errors) == 1
        assert "null" in errors[0]

    def test_reports_a_non_string_instead_of_crashing(self):
        errors = _check(self.rule, self.fields, {"tax_id": 12345678})
        assert len(errors) == 1
        assert "not a string" in errors[0]

    def test_custom_message_wins(self):
        rule = RegexRule(
            field="tax_id", pattern=r"^[A-Z]\d{8}$", message="a CIF is a letter then eight digits"
        )
        assert _check(rule, self.fields, {"tax_id": "nope"}) == [
            "a CIF is a letter then eight digits"
        ]


class TestEvaluateOrder:
    fields = {"total": _spec(FieldType.MONEY), "vendor": _spec()}

    def test_every_rule_runs(self):
        """One failure must not stop the others; the analyst wants the whole list."""
        definition = _definition(
            self.fields,
            [RequiredRule(field="vendor"), PositiveRule(field="total")],
        )
        errors = evaluate(definition, {"vendor": None, "total": Decimal("-1")})
        assert len(errors) == 2

    def test_no_rules_is_a_pass(self):
        assert evaluate(_definition(self.fields), {"vendor": None, "total": None}) == []


# --- The pair together ------------------------------------------------------


class TestValidate:
    definition = _definition(
        {
            "vendor": _spec(),
            "issued_on": _spec(FieldType.DATE),
            "total": _spec(FieldType.MONEY),
            "line_items": _spec(
                FieldType.LIST, item_fields={"amount": _spec(FieldType.MONEY)}
            ),
        },
        [
            RequiredRule(field="vendor"),
            PositiveRule(field="total"),
            NotFutureRule(field="issued_on"),
            LineItemsSumRule(list_field="line_items", item_field="amount", target="total"),
        ],
    )

    def _raw(self, **overrides):
        raw = {
            "vendor": "Cafe Central",
            "issued_on": "2026-05-14",
            "total": "7,15",
            "line_items": [{"amount": "3,60"}, {"amount": "1,80"}, {"amount": "1,75"}],
        }
        return raw | overrides

    def test_a_clean_extraction_produces_no_errors(self):
        """An empty list is what `Extraction.validation_errors` stores on a pass."""
        data, errors = validate(self.definition, self._raw())
        assert errors == []
        assert data["total"] == Decimal("7.15")
        assert data["issued_on"] == date(2026, 5, 14)

    def test_returns_the_coerced_data_as_well_as_the_errors(self):
        data, _ = validate(self.definition, self._raw(vendor=None))
        assert data["total"] == Decimal("7.15")

    def test_rules_see_coerced_values(self):
        """The rules compare Decimals; a comma string would never add up."""
        _, errors = validate(self.definition, self._raw(total="7,16"))
        assert errors == []  # one cent out, inside tolerance

    def test_a_rule_failure_is_reported(self):
        _, errors = validate(self.definition, self._raw(total="9,00"))
        assert len(errors) == 1

    def test_coercion_and_rule_errors_arrive_together(self):
        """
        A malformed amount is two findings: the shape, and the sum that can no
        longer be checked. Both are true and both are actionable.
        """
        _, errors = validate(self.definition, self._raw(total="n/a", vendor=""))
        assert any(e.startswith("total:") for e in errors)
        assert any("vendor is required" in e for e in errors)
        assert any("total is null" in e for e in errors)

    def test_coercion_errors_come_first(self):
        _, errors = validate(self.definition, self._raw(total="n/a"))
        assert errors[0].startswith("total:")

    def test_nothing_extracted_at_all(self):
        """The degenerate case still reports rather than raising."""
        data, errors = validate(self.definition, {})
        assert data == dict.fromkeys(["vendor", "issued_on", "total", "line_items"])
        # One per rule, each naming the null it could not work with, and no
        # coercion errors -- an absent key is not a malformed value.
        assert len(errors) == 4
        assert all("null" in e or "required" in e for e in errors)
