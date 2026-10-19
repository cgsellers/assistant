"""
The eval harness: label loading and scoring.

Both halves are testable without a corpus and without a model, which is the
point -- the harness has to be trustworthy before any number it produces is.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
import yaml

from doctypes.definition import Definition
from evals.labels import Label, check, check_corpus, load_corpus
from evals.score import DocumentScore, Outcome, Report, score_document

DEFINITION = Definition.model_validate(
    {
        "name": "receipt",
        "matches": {"keywords": ["total"]},
        "fields": {
            "vendor": {"type": "string", "description": "The vendor."},
            "issued_on": {"type": "date", "description": "The date."},
            "total": {"type": "money", "description": "The total.", "required": True},
            "currency": {
                "type": "string",
                "description": "The currency.",
                "choices": ["EUR", "USD"],
            },
            "line_items": {
                "type": "list",
                "description": "The items.",
                "item_fields": {
                    "name": {"type": "string", "description": "Name."},
                    "amount": {"type": "money", "description": "Amount."},
                },
            },
        },
    }
)

GOOD_LABEL = {
    "doc_type": "receipt",
    "ocr_text": "TOTAL 8,30",
    "expect": {
        "vendor": "CAFE LUNA",
        "issued_on": "2026-05-12",
        "total": "8,30",
        "currency": None,
        "line_items": [{"name": "Cafe", "amount": "3,60"}],
    },
}


def _write(directory: Path, name: str, body: dict | str) -> Path:
    path = directory / f"{name}.yaml"
    path.write_text(body if isinstance(body, str) else yaml.safe_dump(body))
    return path


class TestLoading:
    def test_a_good_label_loads(self, tmp_path):
        _write(tmp_path, "luna-001", GOOD_LABEL)
        corpus = load_corpus(tmp_path)

        assert len(corpus) == 1
        assert corpus[0].name == "luna-001"
        assert corpus[0].label.expect["total"] == "8,30"

    def test_labels_are_sorted_so_runs_are_comparable(self, tmp_path):
        for name in ["c", "a", "b"]:
            _write(tmp_path, name, GOOD_LABEL)
        assert [d.name for d in load_corpus(tmp_path)] == ["a", "b", "c"]

    def test_an_empty_directory_is_not_an_error(self, tmp_path):
        assert load_corpus(tmp_path) == []

    def test_an_empty_file_names_itself(self, tmp_path):
        _write(tmp_path, "blank", "")
        with pytest.raises(ValueError, match="blank.yaml is empty"):
            load_corpus(tmp_path)

    def test_an_unknown_top_level_key_is_refused(self, tmp_path):
        _write(tmp_path, "typo", GOOD_LABEL | {"expects": {}})
        with pytest.raises(ValueError, match="typo.yaml"):
            load_corpus(tmp_path)

    def test_a_label_with_nothing_to_read_is_refused(self):
        with pytest.raises(ValueError, match="source. or .ocr_text"):
            Label.model_validate({"doc_type": "receipt", "expect": {}})

    def test_cached_ocr_text_is_enough(self):
        assert Label.model_validate(
            {"doc_type": "receipt", "expect": {}, "ocr_text": "TOTAL"}
        ).ocr_text

    def test_a_source_file_is_enough(self):
        assert Label.model_validate(
            {"doc_type": "receipt", "expect": {}, "source": "a.png"}
        ).source


class TestFindingTheDocument:
    def test_the_stem_is_matched_against_known_suffixes(self, tmp_path):
        _write(tmp_path, "luna", GOOD_LABEL)
        (tmp_path / "luna.png").write_bytes(b"x")
        assert load_corpus(tmp_path)[0].document_path().name == "luna.png"

    def test_an_explicit_source_wins(self, tmp_path):
        _write(tmp_path, "luna", GOOD_LABEL | {"source": "scan.pdf"})
        (tmp_path / "luna.png").write_bytes(b"x")
        (tmp_path / "scan.pdf").write_bytes(b"x")
        assert load_corpus(tmp_path)[0].document_path().name == "scan.pdf"

    def test_a_text_only_label_has_no_document(self, tmp_path):
        _write(tmp_path, "luna", GOOD_LABEL)
        assert load_corpus(tmp_path)[0].document_path() is None


class TestChecking:
    def _doc(self, tmp_path, body):
        _write(tmp_path, "x", body)
        return load_corpus(tmp_path)[0]

    def test_a_clean_label_has_no_problems(self, tmp_path):
        assert check(self._doc(tmp_path, GOOD_LABEL), DEFINITION) == []

    def test_an_unknown_field_is_caught(self, tmp_path):
        body = GOOD_LABEL | {"expect": {"total": "8,30", "nonsense": 1}}
        problems = check(self._doc(tmp_path, body), DEFINITION)
        assert any("unknown field" in p and "nonsense" in p for p in problems)

    def test_a_mismatched_doc_type_is_caught(self, tmp_path):
        problems = check(self._doc(tmp_path, GOOD_LABEL | {"doc_type": "invoice"}), DEFINITION)
        assert any("invoice" in p for p in problems)

    def test_a_bad_choice_is_caught(self, tmp_path):
        body = GOOD_LABEL | {"expect": {"total": "8,30", "currency": "GBP"}}
        problems = check(self._doc(tmp_path, body), DEFINITION)
        assert any("GBP" in p for p in problems)

    def test_every_problem_names_the_label(self, tmp_path):
        body = GOOD_LABEL | {"doc_type": "invoice", "expect": {"nope": 1}}
        assert all(p.startswith("x: ") for p in check(self._doc(tmp_path, body), DEFINITION))

    def test_check_corpus_reports_across_files(self, tmp_path):
        _write(tmp_path, "a", GOOD_LABEL | {"doc_type": "invoice"})
        _write(tmp_path, "b", GOOD_LABEL | {"doc_type": "invoice"})
        assert len(check_corpus(load_corpus(tmp_path), DEFINITION)) == 2


class TestTheYamlFloatTrap:
    """
    An unquoted 8.30 in YAML is a float, and the trailing zero is gone before
    anything in this project sees it. Refusing is the only honest response.
    """

    def _problems(self, tmp_path, text):
        (tmp_path / "x.yaml").write_text(text)
        return check(load_corpus(tmp_path)[0], DEFINITION)

    def test_an_unquoted_money_value_is_refused(self, tmp_path):
        problems = self._problems(
            tmp_path, "doc_type: receipt\nocr_text: t\nexpect:\n  total: 8.30\n"
        )
        assert len(problems) == 1
        assert "float" in problems[0]
        assert "'8.30'" in problems[0]  # the message shows the fix

    def test_a_quoted_money_value_is_fine(self, tmp_path):
        assert self._problems(
            tmp_path, "doc_type: receipt\nocr_text: t\nexpect:\n  total: '8.30'\n"
        ) == []

    def test_a_comma_decimal_needs_no_quoting_but_is_accepted_either_way(self, tmp_path):
        assert self._problems(
            tmp_path, "doc_type: receipt\nocr_text: t\nexpect:\n  total: '8,30'\n"
        ) == []

    def test_the_trap_is_caught_inside_list_items_too(self, tmp_path):
        problems = self._problems(
            tmp_path,
            "doc_type: receipt\nocr_text: t\nexpect:\n"
            "  total: '8,30'\n  line_items:\n    - {name: Cafe, amount: 3.60}\n",
        )
        assert len(problems) == 1
        assert "line_items[0].amount" in problems[0]

    def test_an_integer_is_not_mistaken_for_the_trap(self, tmp_path):
        """`total: 8` loses nothing, so there is nothing to refuse."""
        assert self._problems(
            tmp_path, "doc_type: receipt\nocr_text: t\nexpect:\n  total: 8\n"
        ) == []


class TestOutcomes:
    def _score(self, expect, actual) -> dict[str, Outcome]:
        s = score_document(DEFINITION, expect, actual, name="x")
        return {f.path: f.outcome for f in s.fields}

    def test_a_matching_value_is_correct(self):
        assert self._score({"vendor": "Luna"}, {"vendor": "Luna"})["vendor"] == Outcome.CORRECT

    def test_both_null_is_absent_ok(self):
        assert self._score({"vendor": None}, {"vendor": None})["vendor"] == Outcome.ABSENT_OK

    def test_a_value_the_model_did_not_find_is_missed(self):
        assert self._score({"vendor": "Luna"}, {"vendor": None})["vendor"] == Outcome.MISSED

    def test_a_value_the_document_does_not_have_is_hallucinated(self):
        """The currency case: nothing printed, and the model answered anyway."""
        got = self._score({"currency": None}, {"currency": "EUR"})
        assert got["currency"] == Outcome.HALLUCINATED

    def test_two_different_values_are_wrong(self):
        assert self._score({"vendor": "Luna"}, {"vendor": "Sol"})["vendor"] == Outcome.WRONG

    def test_missed_and_hallucinated_are_not_both_just_wrong(self):
        """They need different fixes, so they must not aggregate together."""
        assert Outcome.MISSED != Outcome.WRONG
        assert Outcome.HALLUCINATED != Outcome.WRONG


class TestComparisonIsForgivingWhereItShouldBe:
    def _outcome(self, field_name, expected, actual):
        s = score_document(DEFINITION, {field_name: expected}, {field_name: actual}, name="x")
        return next(f.outcome for f in s.fields if f.path == field_name)

    def test_money_separators_do_not_count_as_a_difference(self):
        assert self._outcome("total", "8,30", "8.30") == Outcome.CORRECT

    def test_a_bare_number_matches_a_string(self):
        assert self._outcome("total", "8.30", 8.30) == Outcome.CORRECT

    def test_trailing_zeros_do_not_count(self):
        assert self._outcome("total", "8.3", "8.30") == Outcome.CORRECT

    def test_case_does_not_count(self):
        assert self._outcome("vendor", "CAFE LUNA", "Cafe Luna") == Outcome.CORRECT

    def test_stretched_whitespace_does_not_count(self):
        assert self._outcome("vendor", "CAFE LUNA", "CAFE   LUNA\n") == Outcome.CORRECT

    def test_accents_do_count(self):
        """A different letter is a different answer, not a formatting choice."""
        assert self._outcome("vendor", "Cafeteria", "Cafetería") == Outcome.WRONG

    def test_a_different_amount_still_counts(self):
        assert self._outcome("total", "8,30", "8,31") == Outcome.WRONG

    def test_dates_compare_as_dates(self):
        assert self._outcome("issued_on", "2026-05-12", "2026-05-12") == Outcome.CORRECT
        assert self._outcome("issued_on", "2026-05-12", "2026-12-05") == Outcome.WRONG


class TestLineItems:
    def _paths(self, expected, actual):
        s = score_document(
            DEFINITION, {"line_items": expected}, {"line_items": actual}, name="x"
        )
        return {f.path: f.outcome for f in s.fields if f.path.startswith("line_items")}

    def test_entries_are_compared_by_position(self):
        got = self._paths(
            [{"name": "Cafe", "amount": "3,60"}, {"name": "Pan", "amount": "1,20"}],
            [{"name": "Cafe", "amount": "3,60"}, {"name": "Pan", "amount": "1,20"}],
        )
        assert set(got.values()) == {Outcome.CORRECT}
        assert "line_items[1].amount" in got

    def test_a_dropped_entry_shows_as_missed_fields(self):
        got = self._paths(
            [{"name": "Cafe", "amount": "3,60"}, {"name": "Pan", "amount": "1,20"}],
            [{"name": "Cafe", "amount": "3,60"}],
        )
        assert got["line_items[1].name"] == Outcome.MISSED
        assert got["line_items[1].amount"] == Outcome.MISSED

    def test_an_invented_entry_shows_as_hallucinated_fields(self):
        got = self._paths(
            [{"name": "Cafe", "amount": "3,60"}],
            [{"name": "Cafe", "amount": "3,60"}, {"name": "Pan", "amount": "1,20"}],
        )
        assert got["line_items[1].name"] == Outcome.HALLUCINATED

    def test_no_items_on_either_side_is_one_pass_not_zero_findings(self):
        got = self._paths([], [])
        assert got == {"line_items": Outcome.ABSENT_OK}

    def test_expected_items_and_none_returned(self):
        got = self._paths([{"name": "Cafe", "amount": "3,60"}], [])
        assert got["line_items[0].name"] == Outcome.MISSED


class TestDocumentScore:
    def test_accuracy_counts_passing_fields(self):
        s = score_document(
            DEFINITION,
            {"vendor": "Luna", "total": "8,30"},
            {"vendor": "Sol", "total": "8,30"},
            name="x",
        )
        assert 0 < s.accuracy < 1

    def test_a_perfect_document_scores_one(self):
        s = score_document(DEFINITION, GOOD_LABEL["expect"], GOOD_LABEL["expect"], name="x")
        assert s.accuracy == 1.0

    def test_routing_is_tracked_separately_from_fields(self):
        s = score_document(DEFINITION, {}, {}, name="x", actual_type="invoice")
        assert s.routed_correctly is False

    def test_nothing_matching_is_a_routing_failure(self):
        s = score_document(DEFINITION, {}, {}, name="x", actual_type=None)
        assert s.routed_correctly is False


class TestReport:
    def _report(self) -> Report:
        perfect = score_document(DEFINITION, {"vendor": "Luna"}, {"vendor": "Luna"}, name="a")
        wrong = score_document(DEFINITION, {"vendor": "Luna"}, {"vendor": "Sol"}, name="b")
        return Report(
            documents=[
                DocumentScore(
                    name=d.name,
                    expected_type="receipt",
                    actual_type="receipt",
                    fields=d.fields,
                    cost_usd=Decimal("0.002312"),
                )
                for d in (perfect, wrong)
            ]
        )

    def test_per_field_outcomes_are_tallied(self):
        counts = self._report().field_outcomes["vendor"]
        assert counts[Outcome.CORRECT] == 1
        assert counts[Outcome.WRONG] == 1

    def test_per_field_accuracy_is_reported(self):
        assert self._report().field_accuracy["vendor"] == 0.5

    def test_list_indices_collapse_so_entries_aggregate(self):
        s = score_document(
            DEFINITION,
            {"line_items": [{"name": "a", "amount": "1,00"}, {"name": "b", "amount": "2,00"}]},
            {"line_items": [{"name": "a", "amount": "1,00"}, {"name": "b", "amount": "2,00"}]},
            name="x",
        )
        report = Report(documents=[DocumentScore("x", "receipt", "receipt", s.fields)])
        assert report.field_outcomes["line_items[].name"][Outcome.CORRECT] == 2

    def test_cost_is_summed_and_averaged_in_decimal(self):
        report = self._report()
        assert report.total_cost == Decimal("0.004624")
        assert report.cost_per_document == Decimal("0.002312")
        assert isinstance(report.total_cost, Decimal)

    def test_routing_accuracy_is_separate_from_field_accuracy(self):
        assert self._report().routing_accuracy == 1.0

    def test_an_empty_report_does_not_divide_by_zero(self):
        empty = Report(documents=[])
        assert empty.accuracy == 1.0
        assert empty.routing_accuracy == 1.0
        assert empty.cost_per_document == Decimal("0")
