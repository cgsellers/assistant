"""
The eval runner.

Everything here uses fakes: a run that needed the network could not be part
of the suite, and the harness has to be trustworthy before any number it
prints means anything.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
import yaml

from doctypes import registry, seed
from evals import run as runner
from evals.labels import load_corpus
from evals.run import CorpusError, format_report, report_as_dict, resolve_text, run_corpus
from evals.score import DocumentScore, Outcome, Report
from llm.base import ExtractionError

from .conftest import FakeExtractor, ForbiddenExtractor

OCR_TEXT = "CAFE SUNRISE\nCIF B12345678\nTICKET 1  03/04/2026\nTOTAL  5,90\nEFECTIVO 6,00\n"

LABEL = {
    "doc_type": "receipt",
    "ocr_text": OCR_TEXT,
    "expect": {"total": "5,90", "vendor": "CAFE SUNRISE", "currency": None},
}

ANSWER = {"total": "5,90", "vendor": "CAFE SUNRISE", "currency": None}


@pytest.fixture(autouse=True)
def _cold_registry():
    registry.clear_cache()
    yield
    registry.clear_cache()


@pytest.fixture
def seeded(session):
    seed.sync(session, seed.load_seeds())
    return session


@pytest.fixture
def corpus(tmp_path):
    def _make(**labels):
        for name, body in (labels or {"a": LABEL}).items():
            (tmp_path / f"{name}.yaml").write_text(yaml.safe_dump(body))
        return tmp_path

    return _make


class TestRunning:
    def test_it_scores_every_document(self, seeded, corpus):
        directory = corpus(a=LABEL, b=LABEL)
        report = run_corpus(seeded, directory, extractor=FakeExtractor(ANSWER))

        assert len(report.documents) == 2
        assert report.accuracy == 1.0
        assert report.routing_accuracy == 1.0

    def test_cost_comes_from_the_extractor_not_an_estimate(self, seeded, corpus):
        report = run_corpus(seeded, corpus(), extractor=FakeExtractor(ANSWER))
        assert report.total_cost == Decimal("0.001205")
        assert isinstance(report.total_cost, Decimal)

    def test_limit_stops_early_so_a_trial_run_is_cheap(self, seeded, corpus):
        extractor = FakeExtractor(ANSWER)
        directory = corpus(a=LABEL, b=LABEL, c=LABEL)

        report = run_corpus(seeded, directory, extractor=extractor, limit=1)

        assert len(report.documents) == 1
        assert len(extractor.calls) == 1

    def test_a_wrong_field_shows_up_as_wrong(self, seeded, corpus):
        report = run_corpus(
            seeded, corpus(), extractor=FakeExtractor(ANSWER | {"vendor": "SOMEWHERE ELSE"})
        )
        outcomes = {f.path: f.outcome for f in report.documents[0].fields}
        assert outcomes["vendor"] == Outcome.WRONG

    def test_an_invented_value_is_reported_as_hallucinated(self, seeded, corpus):
        report = run_corpus(
            seeded, corpus(), extractor=FakeExtractor(ANSWER | {"currency": "EUR"})
        )
        outcomes = {f.path: f.outcome for f in report.documents[0].fields}
        assert outcomes["currency"] == Outcome.HALLUCINATED


class TestItRefusesToRunOnABadCorpus:
    """
    Every document costs money, so the checks happen before the first call,
    not as failures partway through.
    """

    def test_an_empty_directory_is_refused(self, seeded, tmp_path):
        with pytest.raises(CorpusError, match="no labels found"):
            run_corpus(seeded, tmp_path, extractor=ForbiddenExtractor())

    def test_a_label_naming_an_unknown_field_is_refused_before_any_call(
        self, seeded, corpus
    ):
        directory = corpus(a=LABEL | {"expect": {"total": "5,90", "nonsense": 1}})
        # ForbiddenExtractor raises if called, so this also proves nothing was spent.
        with pytest.raises(CorpusError, match="unknown field"):
            run_corpus(seeded, directory, extractor=ForbiddenExtractor())

    def test_an_unquoted_money_label_is_refused(self, seeded, tmp_path):
        (tmp_path / "a.yaml").write_text(
            "doc_type: receipt\nocr_text: t\nexpect:\n  total: 5.90\n"
        )
        with pytest.raises(CorpusError, match="float"):
            run_corpus(seeded, tmp_path, extractor=ForbiddenExtractor())

    def test_every_problem_is_reported_at_once(self, seeded, corpus):
        bad = LABEL | {"expect": {"nope": 1}}
        with pytest.raises(CorpusError) as caught:
            run_corpus(seeded, corpus(a=bad, b=bad), extractor=ForbiddenExtractor())
        assert "a:" in str(caught.value)
        assert "b:" in str(caught.value)

    def test_an_unseeded_database_says_what_to_run(self, session, corpus):
        with pytest.raises(CorpusError, match="doctypes.seed"):
            run_corpus(session, corpus(), extractor=ForbiddenExtractor())

    def test_a_failed_extraction_stops_the_run(self, seeded, corpus):
        """
        Scoring a failed call as "answered nothing" would read as a perfect
        score for every field the label expects to be null.
        """
        extractor = FakeExtractor(raises=ExtractionError("rate limited"))
        with pytest.raises(CorpusError, match="rate limited"):
            run_corpus(seeded, corpus(), extractor=extractor)


class TestRoutingIsMeasuredNotAssumed:
    def test_a_document_nothing_claims_is_a_routing_failure(self, seeded, corpus):
        directory = corpus(a=LABEL | {"ocr_text": "Dear Sir, please find enclosed"})
        extractor = ForbiddenExtractor()

        report = run_corpus(seeded, directory, extractor=extractor)

        document = report.documents[0]
        assert document.routed_correctly is False
        assert document.actual_type is None
        assert document.fields == []  # nothing to score; it was never extracted

    def test_routing_accuracy_is_separate_from_field_accuracy(self, seeded, corpus):
        directory = corpus(
            good=LABEL, bad=LABEL | {"ocr_text": "Dear Sir, please find enclosed"}
        )
        report = run_corpus(seeded, directory, extractor=FakeExtractor(ANSWER))

        assert report.routing_accuracy == 0.5
        # The one that did route scored perfectly; routing did not drag it down.
        assert report.accuracy == 1.0


class TestResolvingText:
    def test_cached_text_is_preferred_and_needs_no_engine(self, corpus):
        document = load_corpus(corpus())[0]
        resolved = resolve_text(document)
        assert resolved.from_cache is True
        assert resolved.text == OCR_TEXT

    def test_a_label_with_neither_text_nor_file_is_an_error(self, tmp_path):
        (tmp_path / "a.yaml").write_text(
            "doc_type: receipt\nsource: missing.png\nexpect: {}\n"
        )
        with pytest.raises(CorpusError, match="no cached ocr_text"):
            resolve_text(load_corpus(tmp_path)[0])

    def test_an_engine_reads_the_source_file_instead(self, tmp_path):
        from .conftest import TINY_PNG, FakeEngine

        (tmp_path / "a.png").write_bytes(TINY_PNG)
        (tmp_path / "a.yaml").write_text("doc_type: receipt\nsource: a.png\nexpect: {}\n")
        engine = FakeEngine()

        resolved = resolve_text(load_corpus(tmp_path)[0], engine)

        assert resolved.from_cache is False
        assert resolved.text == engine.output.full_text
        assert engine.calls  # it really went through OCR

    def test_a_source_file_without_an_engine_says_so(self, tmp_path):
        from .conftest import TINY_PNG

        (tmp_path / "a.png").write_bytes(TINY_PNG)
        (tmp_path / "a.yaml").write_text("doc_type: receipt\nsource: a.png\nexpect: {}\n")
        with pytest.raises(CorpusError, match="OCR engine is required"):
            resolve_text(load_corpus(tmp_path)[0])


class TestTheBundledExamples:
    """
    The examples are committed, so they are documentation as much as fixtures.
    Documentation that no longer parses is worse than none.
    """

    def test_they_all_load(self):
        assert len(load_corpus(runner.EXAMPLES_DIR)) >= 3

    def test_they_check_clean_against_the_shipped_receipt_type(self):
        from evals.labels import check_corpus

        definition = next(d for d in seed.load_seeds() if d.name == "receipt")
        assert check_corpus(load_corpus(runner.EXAMPLES_DIR), definition) == []

    def test_each_one_routes_to_receipt(self, seeded):
        for document in load_corpus(runner.EXAMPLES_DIR):
            match = registry.route(seeded, document.label.ocr_text)
            assert match is not None, f"{document.name} routes nowhere"
            assert match.definition.name == "receipt"

    def test_one_of_them_expects_a_null_currency(self):
        """The invention trap only works if something expects nothing."""
        corpus = load_corpus(runner.EXAMPLES_DIR)
        assert any(d.label.expect.get("currency") is None for d in corpus)

    def test_one_of_them_expects_a_real_currency(self):
        """...and the opposite, or a model answering null always would score well."""
        corpus = load_corpus(runner.EXAMPLES_DIR)
        assert any(d.label.expect.get("currency") is not None for d in corpus)


class TestFormatting:
    def _report(self) -> Report:
        from evals.score import score_document

        definition = next(d for d in seed.load_seeds() if d.name == "receipt")
        scored = score_document(
            definition,
            {"total": "5,90", "currency": None},
            {"total": "5,90", "currency": "EUR"},
            name="a",
        )
        return Report(
            documents=[
                DocumentScore(
                    name="a",
                    expected_type="receipt",
                    actual_type="receipt",
                    fields=scored.fields,
                    cost_usd=Decimal("0.002171"),
                    latency_ms=1523,
                )
            ]
        )

    def test_the_table_names_every_field(self):
        text = format_report(self._report())
        assert "currency" in text
        assert "total" in text

    def test_invented_values_are_called_out_by_name(self):
        """The whole point: a hallucination should be impossible to skim past."""
        text = format_report(self._report())
        assert "invented values" in text
        assert "currency = 'EUR'" in text

    def test_cost_is_reported(self):
        assert "0.002171" in format_report(self._report())

    def test_json_is_machine_readable(self):
        data = report_as_dict(self._report())
        assert data["documents"] == 1
        assert data["total_cost_usd"] == "0.002171"
        assert data["per_field"]["currency"]["hallucinated"] == 1

    def test_an_empty_report_formats_without_crashing(self):
        assert "0 document(s)" in format_report(Report(documents=[]))
