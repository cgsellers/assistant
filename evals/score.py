"""
Scoring an extraction against a label.

The unit is a field, not a document. "87% of documents were perfect" hides
which field is costing the 13%, and the whole point of a per-field schema is
that fields fail independently -- a vendor name that is nearly always right
and a tax id that is nearly always wrong are two different problems with two
different fixes.

Five outcomes, because "wrong" is not one thing:

    correct       expected a value, got that value
    absent_ok     expected nothing, got nothing
    missed        expected a value, got null
    hallucinated  expected nothing, got a value
    wrong         both present, and they differ

`missed` and `hallucinated` are the pair worth separating. A missed field is
a description that needs to point harder at the page. A hallucinated one is
a model answering from the world rather than the document, which no amount
of description fixes and which a finance system cannot tolerate quietly --
an invented tax id looks exactly like a real one downstream.

Both sides are coerced before comparison, using the same code the pipeline
uses, so `'7,15'` and `'7.15'` and `7.15` are one value and a label is not
required to guess which spelling the model will pick.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from typing import Any

from doctypes.definition import Definition, FieldSpec, FieldType
from doctypes.validate import coerce


class Outcome(StrEnum):
    CORRECT = "correct"
    ABSENT_OK = "absent_ok"
    MISSED = "missed"
    HALLUCINATED = "hallucinated"
    WRONG = "wrong"


#: The outcomes that mean the model got it right. Kept as a set rather than
#: `!= WRONG` so that adding an outcome forces a decision about which side it
#: falls on.
PASSING = frozenset({Outcome.CORRECT, Outcome.ABSENT_OK})


@dataclass(frozen=True)
class FieldScore:
    """One field, or one field of one list entry."""

    #: Dotted path: `total`, or `line_items[1].amount`.
    path: str
    outcome: Outcome
    expected: Any = None
    actual: Any = None

    @property
    def passed(self) -> bool:
        return self.outcome in PASSING


@dataclass(frozen=True)
class DocumentScore:
    name: str
    #: What the label said the type was, and what routing actually chose.
    #: None means nothing matched, which is a routing failure rather than an
    #: extraction one and is counted separately.
    expected_type: str
    actual_type: str | None
    fields: list[FieldScore] = field(default_factory=list)
    cost_usd: Decimal | None = None
    latency_ms: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None

    @property
    def routed_correctly(self) -> bool:
        return self.actual_type == self.expected_type

    @property
    def accuracy(self) -> float:
        """Share of fields that came out right. 1.0 for a document with none."""
        if not self.fields:
            return 1.0
        return sum(f.passed for f in self.fields) / len(self.fields)


#: A sentinel, because None is a real answer for `actual_type`: it means
#: routing matched nothing at all, which is a different outcome from a caller
#: that simply did not say. Sharing one value for both would silently score a
#: routing failure as a success.
_ROUTING_NOT_GIVEN = object()


def score_document(
    definition: Definition,
    expect: dict,
    actual: dict,
    *,
    name: str,
    actual_type: str | None = _ROUTING_NOT_GIVEN,  # type: ignore[assignment]
) -> DocumentScore:
    """
    Compare one extraction against one label.

    `expect` and `actual` are both raw -- the label as written and the JSON
    the model returned -- and both are coerced here. Coercion also fills in
    every declared field, so a key absent from one side and null on the other
    compare equal, which they should: both mean "not there".
    """
    expected_values, _ = coerce(definition, expect)
    actual_values, _ = coerce(definition, actual)

    scores: list[FieldScore] = []
    for field_name, spec in definition.fields.items():
        scores.extend(
            _score_field(
                spec, expected_values.get(field_name), actual_values.get(field_name), field_name
            )
        )

    return DocumentScore(
        name=name,
        expected_type=definition.name,
        actual_type=(
            definition.name if actual_type is _ROUTING_NOT_GIVEN else actual_type
        ),
        fields=scores,
    )


def _score_field(spec: FieldSpec, expected: Any, actual: Any, path: str) -> list[FieldScore]:
    if spec.type is FieldType.LIST:
        return _score_list(spec, expected, actual, path)
    return [FieldScore(path, _outcome(spec, expected, actual), expected, actual)]


def _score_list(spec: FieldSpec, expected: Any, actual: Any, path: str) -> list[FieldScore]:
    """
    Entries are compared by position, which is what a receipt means by order.

    A dropped entry therefore shifts everything after it and shows up as a
    run of failures rather than one. That is the honest reading: the
    extraction no longer says what the document says, and pretending a
    best-effort re-alignment recovered it would flatter the number.
    """
    assert spec.item_fields is not None
    expected_items = expected if isinstance(expected, list) else []
    actual_items = actual if isinstance(actual, list) else []

    # Nothing expected and nothing produced is a pass for the list as a whole,
    # reported once rather than as zero findings.
    if not expected_items and not actual_items:
        return [FieldScore(path, Outcome.ABSENT_OK, None, None)]

    scores: list[FieldScore] = []
    for i in range(max(len(expected_items), len(actual_items))):
        expected_entry = expected_items[i] if i < len(expected_items) else None
        actual_entry = actual_items[i] if i < len(actual_items) else None
        for key, item_spec in spec.item_fields.items():
            scores.extend(
                _score_field(
                    item_spec,
                    (expected_entry or {}).get(key),
                    (actual_entry or {}).get(key),
                    f"{path}[{i}].{key}",
                )
            )
    return scores


def _outcome(spec: FieldSpec, expected: Any, actual: Any) -> Outcome:
    if expected is None and actual is None:
        return Outcome.ABSENT_OK
    if expected is None:
        return Outcome.HALLUCINATED
    if actual is None:
        return Outcome.MISSED
    return Outcome.CORRECT if _same(spec, expected, actual) else Outcome.WRONG


def _same(spec: FieldSpec, expected: Any, actual: Any) -> bool:
    if spec.type is FieldType.STRING:
        return _normalise(expected) == _normalise(actual)
    return expected == actual


def _normalise(value: Any) -> str:
    """
    Case and whitespace do not count as a wrong answer.

    "Cafe Central" and "CAFE CENTRAL" are the same vendor, and which one comes
    back depends on how the receipt was printed rather than on whether the
    model read it correctly. Accents and punctuation are left alone: those do
    distinguish real values.
    """
    return re.sub(r"\s+", " ", str(value)).strip().casefold()


@dataclass(frozen=True)
class Report:
    """What a whole run came to."""

    documents: list[DocumentScore]

    @property
    def field_outcomes(self) -> dict[str, Counter]:
        """field path (list indices collapsed) -> outcome counts."""
        tally: dict[str, Counter] = {}
        for document in self.documents:
            for score in document.fields:
                tally.setdefault(_collapse(score.path), Counter())[score.outcome] += 1
        return tally

    @property
    def field_accuracy(self) -> dict[str, float]:
        return {
            path: sum(counts[o] for o in PASSING) / sum(counts.values())
            for path, counts in self.field_outcomes.items()
        }

    @property
    def accuracy(self) -> float:
        scored = [s for d in self.documents for s in d.fields]
        return sum(s.passed for s in scored) / len(scored) if scored else 1.0

    @property
    def routing_accuracy(self) -> float:
        if not self.documents:
            return 1.0
        return sum(d.routed_correctly for d in self.documents) / len(self.documents)

    @property
    def total_cost(self) -> Decimal:
        return sum((d.cost_usd or Decimal("0") for d in self.documents), Decimal("0"))

    @property
    def cost_per_document(self) -> Decimal:
        if not self.documents:
            return Decimal("0")
        return self.total_cost / len(self.documents)


def _collapse(path: str) -> str:
    """`line_items[3].amount` -> `line_items[].amount`, so entries aggregate."""
    return re.sub(r"\[\d+\]", "[]", path)


@dataclass(frozen=True)
class Change:
    """One field whose outcome differed between two runs."""

    document: str
    path: str
    before: FieldScore
    after: FieldScore

    @property
    def is_regression(self) -> bool:
        return self.before.passed and not self.after.passed

    @property
    def is_improvement(self) -> bool:
        return self.after.passed and not self.before.passed


@dataclass(frozen=True)
class Comparison:
    """
    Two runs over the same corpus, and what moved between them.

    The headline number is the least useful part. "Accuracy went up two
    points" can mean three fields got better and one got worse, and the one
    that got worse may be the one that matters -- a model that learns to read
    tax ids and forgets to read totals has not improved. So the regressions
    are listed individually and by name, and they are listed first.
    """

    names: tuple[str, str]
    reports: tuple[Report, Report]

    def _indexed(self, which: int) -> dict[tuple[str, str], FieldScore]:
        return {
            (document.name, score.path): score
            for document in self.reports[which].documents
            for score in document.fields
        }

    @property
    def changes(self) -> list[Change]:
        """
        Every field that moved. Fields only one run scored are skipped: that
        happens when routing differed, which the routing numbers already say
        and which is not a field-level finding.
        """
        before, after = self._indexed(0), self._indexed(1)
        return [
            Change(document=key[0], path=key[1], before=before[key], after=after[key])
            for key in sorted(before.keys() & after.keys())
            if before[key].outcome is not after[key].outcome
        ]

    @property
    def regressions(self) -> list[Change]:
        return [c for c in self.changes if c.is_regression]

    @property
    def improvements(self) -> list[Change]:
        return [c for c in self.changes if c.is_improvement]

    @property
    def field_deltas(self) -> dict[str, tuple[float, float]]:
        """field path -> (accuracy before, accuracy after), for every field."""
        before, after = self.reports[0].field_accuracy, self.reports[1].field_accuracy
        return {
            path: (before.get(path, 0.0), after.get(path, 0.0))
            for path in sorted(before.keys() | after.keys())
        }

    @property
    def accuracy_delta(self) -> float:
        return self.reports[1].accuracy - self.reports[0].accuracy

    @property
    def cost_ratio(self) -> Decimal | None:
        """
        How many times more the second variant cost. None when the first was
        free, which happens with a fake and would otherwise divide by zero.
        """
        first = self.reports[0].total_cost
        if not first:
            return None
        return self.reports[1].total_cost / first
