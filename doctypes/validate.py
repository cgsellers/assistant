"""
Coercion and deterministic rule checking for an extraction.

Two steps, kept separate because they fail for different reasons and the fix is
different. Coercion asks "is this value the shape the definition declared?" and
is about the model's output format. Rule evaluation asks "do these values agree
with each other?" and is about the document. An analyst reading
`validation_errors` needs to tell those apart.

Coercion never raises: a value it cannot parse becomes None and contributes a
message, so one malformed field does not hide what the other twenty say. The
consequence is that a bad value is reported twice -- once for its shape, once by
whatever rule then finds a null -- which is two true statements about one
problem and better than suppressing either.

Money is `Decimal` throughout, including tolerances and sums. The wire format is
a string precisely so no float ever exists to round.
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from doctypes.definition import (
    Definition,
    FieldSpec,
    FieldType,
    LineItemsSumRule,
    NotFutureRule,
    PositiveRule,
    RangeRule,
    RegexRule,
    RequiredRule,
    Rule,
    SumEqualsRule,
)

# Mirrors the money pattern in schema._JSON_TYPE, and must not be loosened
# independently of it. Anything the schema rejects has to be rejected here too:
# a definition writes no thousands separators, so accepting "1,234" would
# normalise it to 1.234 and lose three orders of magnitude silently.
_MONEY_WIRE = re.compile(r"^-?\d+([.,]\d{1,2})?$")

# `date.fromisoformat` has accepted the other ISO spellings since 3.11
# ("20260514", week dates). The schema promises one spelling, so check it before
# parsing rather than quietly accepting a format nothing else in the system
# produces.
_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


# --- Coercion ---------------------------------------------------------------


def coerce(definition: Definition, data: dict) -> tuple[dict, list[str]]:
    """
    Turn raw extracted JSON into typed Python values, plus the list of values
    that could not be typed.

    Every declared field appears in the result, null if absent or unparseable;
    undeclared keys are dropped. Both halves of that are deliberate: downstream
    code indexes the result by the definition's field names and should not have
    to guard each one, and a key the definition does not know about has no type
    to be coerced to and no rule that can reference it.
    """
    if not isinstance(data, dict):
        return (
            dict.fromkeys(definition.fields),
            [f"expected a JSON object, got {type(data).__name__}"],
        )

    coerced: dict[str, Any] = {}
    errors: list[str] = []

    for name, spec in definition.fields.items():
        value, problems = _coerce_value(spec, data.get(name), name)
        coerced[name] = value
        errors.extend(problems)

    return coerced, errors


def _coerce_value(spec: FieldSpec, value: Any, path: str) -> tuple[Any, list[str]]:
    """
    One value against one spec. `path` is the dotted/indexed name used in error
    messages -- an analyst looking at "line_items[2].unit_price" can find the
    cell on the document, "unit_price" alone is ambiguous across eight rows.
    """
    if value is None:
        # Not an error at any depth. A top-level optional field is explicitly
        # nullable, and an item field is simply allowed to be absent from the
        # entry (build_json_schema does not nullable-wrap inside `items`).
        return None, []

    if spec.type is FieldType.LIST:
        return _coerce_list(spec, value, path)

    try:
        return _SCALARS[spec.type](value), []
    except (ValueError, TypeError, InvalidOperation) as exc:
        return None, [f"{path}: {exc}"]


def _coerce_list(spec: FieldSpec, value: Any, path: str) -> tuple[Any, list[str]]:
    if not isinstance(value, list):
        return None, [f"{path}: expected a list of entries, got {type(value).__name__}"]

    assert spec.item_fields is not None  # guaranteed by Definition
    entries: list[dict] = []
    errors: list[str] = []

    for index, entry in enumerate(value):
        if not isinstance(entry, dict):
            errors.append(f"{path}[{index}]: expected an object, got {type(entry).__name__}")
            # Keep a placeholder so positions survive. A later line_items_sum
            # error names an index, and it has to be the index the analyst sees
            # in the raw JSON.
            entries.append(dict.fromkeys(spec.item_fields))
            continue

        coerced: dict[str, Any] = {}
        for name, item_spec in spec.item_fields.items():
            item_value, problems = _coerce_value(
                item_spec, entry.get(name), f"{path}[{index}].{name}"
            )
            coerced[name] = item_value
            errors.extend(problems)
        entries.append(coerced)

    return entries, errors


def _to_money(value: Any) -> Decimal:
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool):
        # bool is an int. A yes/no answer is not an amount.
        raise ValueError(f"expected an amount, got {value!r}")
    if isinstance(value, int | float):
        # The schema asks for a string and the model obliges, but a bare number
        # is unambiguous and not worth failing an otherwise good extraction
        # over. str() is the only float -> Decimal route that does not drag the
        # binary representation along.
        return Decimal(str(value))
    if not isinstance(value, str):
        raise ValueError(f"expected an amount, got {type(value).__name__}")

    text = value.strip()
    if not _MONEY_WIRE.match(text):
        raise ValueError(
            f"expected an amount like '1234.56' or '1234,56', got {value!r}"
        )
    # Comma is the decimal separator, not a group separator -- Spanish
    # documents print "7,15" and the prompt asks for the separator as printed.
    return Decimal(text.replace(",", "."))


def _to_date(value: Any) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if not isinstance(value, str) or not _ISO_DATE.match(value.strip()):
        raise ValueError(f"expected a date as YYYY-MM-DD, got {value!r}")
    try:
        return date.fromisoformat(value.strip())
    except ValueError as exc:
        raise ValueError(f"{value!r} is not a real date: {exc}") from exc


def _to_int(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"expected an integer, got {value!r}")
    return value


def _to_number(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(f"expected a number, got {value!r}")
    return float(value)


def _to_bool(value: Any) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"expected true or false, got {value!r}")
    return value


def _to_str(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError(f"expected a string, got {type(value).__name__}")
    return value


_SCALARS = {
    FieldType.STRING: _to_str,
    FieldType.INTEGER: _to_int,
    FieldType.NUMBER: _to_number,
    FieldType.MONEY: _to_money,
    FieldType.DATE: _to_date,
    FieldType.BOOLEAN: _to_bool,
}


# --- Rule evaluation --------------------------------------------------------


def evaluate(definition: Definition, data: dict) -> list[str]:
    """
    Run every rule against already-coerced data. An empty list is a pass, which
    is the same thing `Extraction.validation_errors == []` means.

    Expects the output of `coerce`: the rules compare dates to dates and
    Decimals to Decimals and do not re-parse strings.
    """
    errors: list[str] = []
    for rule in definition.rules:
        errors.extend(_evaluate_rule(rule, data))
    return errors


def validate(definition: Definition, raw_json: dict) -> tuple[dict, list[str]]:
    """Coerce, then evaluate. The pair that goes into an Extraction row."""
    data, errors = coerce(definition, raw_json)
    return data, errors + evaluate(definition, data)


def _absent(value: Any) -> bool:
    """
    Nothing to check. An empty list counts, matching how `required` reads one:
    a model that found no line items answers `[]` as often as it answers null.
    """
    return value is None or (isinstance(value, list) and not value)


def _referenced_fields(rule: Rule) -> list[str]:
    """Every top-level field a rule reads. Validated to exist by Definition."""
    names: list[str] = []
    for attr in ("field", "target", "list_field"):
        if (name := getattr(rule, attr, None)):
            names.append(name)
    names.extend(getattr(rule, "of", None) or [])
    return names


def _evaluate_rule(rule: Rule, data: dict) -> list[str]:
    if isinstance(rule, RequiredRule):
        return _check_required(rule, data)

    # Every other rule answers "is this value wrong", not "is this value here".
    # `required` is the rule that owns absence, so a rule whose inputs are
    # missing has no question to answer and stays quiet.
    #
    # Without this, a rule on an optional field reports an error every time
    # that field is legitimately absent -- not_future on a receipt whose date
    # did not survive OCR, sum_equals on a till roll that prints no subtotal --
    # and the document goes to review for being incomplete rather than wrong.
    # That makes "optional field plus a rule" an unusable combination, which is
    # most of the useful rules.
    #
    # A malformed value still reports: coercion already flagged its shape
    # before nulling it, so the error says what is actually wrong with it
    # rather than restating that the rule could not run.
    if any(_absent(data.get(name)) for name in _referenced_fields(rule)):
        return []

    if isinstance(rule, PositiveRule):
        return _check_positive(rule, data)
    if isinstance(rule, RangeRule):
        return _check_range(rule, data)
    if isinstance(rule, SumEqualsRule):
        return _check_sum_equals(rule, data)
    if isinstance(rule, LineItemsSumRule):
        return _check_line_items_sum(rule, data)
    if isinstance(rule, NotFutureRule):
        return _check_not_future(rule, data)
    if isinstance(rule, RegexRule):
        return _check_regex(rule, data)
    raise NotImplementedError(f"no evaluator for rule type {rule.type!r}")


def _message(rule: Rule, generated: str) -> str:
    """
    The analyst's wording wins. They know why the rule exists ("the till roll
    always prints a tax id"); the generated text only knows what it compared.
    """
    return rule.message or generated


def _check_required(rule: RequiredRule, data: dict) -> list[str]:
    value = data.get(rule.field)
    # "" and [] count as absent: a model that found nothing sometimes answers
    # with an empty value instead of null, and the analyst means one thing by
    # both. 0 and False are answers, not absences, so test by type rather than
    # truthiness.
    missing = (
        value is None
        or (isinstance(value, str) and not value.strip())
        or (isinstance(value, list) and not value)
    )
    if missing:
        return [_message(rule, f"{rule.field} is required but was not extracted")]
    return []


def _check_positive(rule: PositiveRule, data: dict) -> list[str]:
    number, problem = _number(data, rule.field)
    if problem:
        return [_message(rule, f"{problem}, so it cannot be checked for being positive")]
    assert number is not None
    if number <= 0:
        return [_message(rule, f"{rule.field} must be greater than 0, got {number}")]
    return []


def _check_range(rule: RangeRule, data: dict) -> list[str]:
    if rule.min is None and rule.max is None:
        return []

    number, problem = _number(data, rule.field)
    if problem:
        return [_message(rule, f"{problem}, so it cannot be checked against its range")]
    assert number is not None

    if rule.min is not None and number < _decimal(rule.min):
        return [_message(rule, f"{rule.field} must be at least {_decimal(rule.min)}, got {number}")]
    if rule.max is not None and number > _decimal(rule.max):
        return [_message(rule, f"{rule.field} must be at most {_decimal(rule.max)}, got {number}")]
    return []


def _check_sum_equals(rule: SumEqualsRule, data: dict) -> list[str]:
    label = " + ".join(rule.of)

    parts: list[Decimal] = []
    problems: list[str] = []
    for name in [rule.target, *rule.of]:
        number, problem = _number(data, name)
        if problem:
            problems.append(problem)
        else:
            assert number is not None
            parts.append(number)

    if problems:
        return [_message(rule, f"cannot check {label} = {rule.target}: {'; '.join(problems)}")]

    target, *addends = parts
    total = sum(addends, Decimal(0))
    tolerance = _decimal(rule.tolerance)
    if abs(total - target) > tolerance:
        return [
            _message(
                rule,
                f"{label} = {total}, which does not match {rule.target} "
                f"({target}) within {tolerance}",
            )
        ]
    return []


def _check_line_items_sum(rule: LineItemsSumRule, data: dict) -> list[str]:
    label = f"{rule.list_field}[].{rule.item_field}"

    entries = data.get(rule.list_field)
    target, problem = _number(data, rule.target)
    problems = [problem] if problem else []

    if entries is None:
        problems.append(f"{rule.list_field} is null")
    elif not isinstance(entries, list):
        problems.append(f"{rule.list_field} is not a list")

    if problems:
        return [_message(rule, f"cannot check {label} = {rule.target}: {'; '.join(problems)}")]
    assert isinstance(entries, list) and target is not None

    total = Decimal(0)
    for index, entry in enumerate(entries):
        value = entry.get(rule.item_field) if isinstance(entry, dict) else None
        number = _as_decimal(value)
        if number is None:
            # Not summed as zero: an unreadable line makes the total unknown,
            # not smaller. Say which line, then stop before a comparison that
            # would blame the wrong field.
            where = f"{rule.list_field}[{index}].{rule.item_field}"
            problems.append(f"{where} is {'null' if value is None else 'not a number'}")
        else:
            total += number

    if problems:
        return [_message(rule, f"cannot check {label} = {rule.target}: {'; '.join(problems)}")]

    tolerance = _decimal(rule.tolerance)
    if abs(total - target) > tolerance:
        return [
            _message(
                rule,
                f"{label} sums to {total}, which does not match {rule.target} "
                f"({target}) within {tolerance}",
            )
        ]
    return []


def _check_not_future(rule: NotFutureRule, data: dict) -> list[str]:
    value = data.get(rule.field)
    if value is None:
        return [_message(rule, f"{rule.field} is null, so it cannot be checked against today")]
    if not isinstance(value, date):
        return [_message(rule, f"{rule.field} is not a date, so it cannot be compared to today")]

    # UTC rather than local time, to match how every other timestamp in the
    # system is stored. grace_days already covers the offset either way.
    today = datetime.now(UTC).date()
    limit = today + timedelta(days=rule.grace_days)
    if value > limit:
        return [
            _message(
                rule,
                f"{rule.field} is {value.isoformat()}, which is after today "
                f"({today.isoformat()}) plus {rule.grace_days} day(s) of grace",
            )
        ]
    return []


def _check_regex(rule: RegexRule, data: dict) -> list[str]:
    value = data.get(rule.field)
    if value is None:
        return [_message(rule, f"{rule.field} is null, so it cannot be matched against a pattern")]
    if not isinstance(value, str):
        return [_message(rule, f"{rule.field} is not a string, so it cannot be pattern-matched")]

    # search, not fullmatch, and consistent with the matchers in schema.py. An
    # analyst who wants the whole value to match writes ^...$; under fullmatch
    # the half-anchored patterns people actually write would silently mean
    # something else.
    if not re.search(rule.pattern, value):
        return [_message(rule, f"{rule.field} ({value!r}) does not match {rule.pattern!r}")]
    return []


def _number(data: dict, field: str) -> tuple[Decimal | None, str | None]:
    """
    A field as a Decimal, or a phrase describing why it is not one. Rules report
    the reason instead of crashing: a null total is a finding about the
    document, not a bug in the validator.
    """
    value = data.get(field)
    if value is None:
        return None, f"{field} is null"
    number = _as_decimal(value)
    if number is None:
        return None, f"{field} is not a number ({value!r})"
    return number, None


def _as_decimal(value: Any) -> Decimal | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, int | float):
        return Decimal(str(value))
    return None


def _decimal(value: float) -> Decimal:
    """
    Rule thresholds are floats in the definition format (JSON has nothing else)
    but every comparison they take part in is decimal. str() first, or 0.02
    arrives as 0.0200000000000000004 and a tolerance boundary stops being one.

    Tolerances are in the field's own units, not cents: a receipt in euros with
    tolerance 0.02 accepts a two-cent rounding difference.
    """
    return Decimal(str(value))
