"""
Transforms: a document type's own Python, run on what the model read.

What is under test is the contract around the code rather than any one
transform: that it runs apart from the worker, that its failures are
contained and explained, that whatever it changes is marked as derived, and
that its output faces the same rules as the model's.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
import yaml
from pydantic import ValidationError

from core import repository
from core.models import DocumentTypeDefinition, Extraction, Job, OcrResult, Transaction
from core.status import DocStatus, JobKind
from doctypes import registry, seed
from doctypes.canonical import derived_columns
from doctypes.definition import Definition, Transform
from doctypes.transform import TransformError, check_examples, process, run

from .conftest import TINY_PNG, FakeExtractor, demo_org_id

DOUBLE = (
    "from decimal import Decimal\n\n"
    "def transform(data):\n"
    "    return {**data, 'total': str(Decimal(data['total']) * 2)}\n"
)


def _definition(code: str | None = DOUBLE, **transform) -> Definition:
    spec = {
        "name": "receipt",
        "matches": {"keywords": ["total"]},
        "fields": {
            "total": {"type": "money", "description": "The total."},
            "vendor": {"type": "string", "description": "The vendor."},
        },
        "rules": [{"type": "positive", "field": "total"}],
    }
    if code is not None:
        spec["transform"] = {"code": code, **transform}
    return Definition.model_validate(spec)


class TestDeclaringOne:
    def test_code_that_does_not_parse_is_refused(self):
        with pytest.raises(ValidationError, match="does not parse"):
            _definition("def transform(data)\n    return data\n")

    def test_it_must_define_transform(self):
        with pytest.raises(ValidationError, match="must define"):
            _definition("def convert(data):\n    return data\n")

    def test_transform_takes_exactly_the_data(self):
        with pytest.raises(ValidationError, match="exactly one argument"):
            _definition("def transform(data, extra):\n    return data\n")

    def test_validating_a_definition_never_runs_the_code(self, tmp_path):
        """Definitions are parsed on every load; executing them there would be a bug."""
        marker = tmp_path / "ran"
        _definition(
            f"open({str(marker)!r}, 'w').close()\n\ndef transform(data):\n    return data\n"
        )
        assert not marker.exists()


class TestRunningIt:
    def test_it_returns_what_the_code_returns(self):
        assert run(Transform(code=DOUBLE), {"total": "4.50"}) == {"total": "9.00"}

    def test_an_exception_names_itself_and_its_line(self):
        code = "def transform(data):\n    return data['missing']\n"
        with pytest.raises(TransformError, match=r"KeyError: 'missing' \(line 2\)"):
            run(Transform(code=code), {})

    def test_an_infinite_loop_is_stopped(self):
        code = "def transform(data):\n    while True:\n        pass\n"
        with pytest.raises(TransformError, match="timed out"):
            run(Transform(code=code, timeout_seconds=0.5), {})

    def test_it_must_return_a_dict(self):
        with pytest.raises(TransformError, match="must return a dict"):
            run(Transform(code="def transform(data):\n    return [data]\n"), {})

    def test_a_stray_print_does_not_corrupt_the_result(self):
        code = "def transform(data):\n    print('debugging')\n    return data\n"
        assert run(Transform(code=code), {"a": 1}) == {"a": 1}

    def test_decimal_and_date_come_back_in_wire_format(self):
        code = (
            "from datetime import date\nfrom decimal import Decimal\n\n"
            "def transform(data):\n"
            "    return {'total': Decimal('10.500'), 'on': date(2026, 3, 1)}\n"
        )
        assert run(Transform(code=code), {}) == {"total": "10.5", "on": "2026-03-01"}

    def test_it_does_not_inherit_the_workers_environment(self, monkeypatch):
        """The worker holds the API key; a transform has no business seeing it."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-secret")
        code = (
            "import os\n\ndef transform(data):\n"
            "    return {'key': os.environ.get('ANTHROPIC_API_KEY')}\n"
        )
        assert run(Transform(code=code), {}) == {"key": None}

    def test_only_the_standard_library_is_importable(self):
        code = "import pydantic\n\ndef transform(data):\n    return data\n"
        with pytest.raises(TransformError, match="ModuleNotFoundError"):
            run(Transform(code=code), {})


class TestProcessing:
    def test_without_a_transform_it_is_coerce_then_rules(self):
        processed = process(_definition(None), {"total": "-1.00"})
        assert processed.data["total"] == Decimal("-1.00")
        assert processed.transformed is None
        assert processed.derived == []
        assert any("must be greater than 0" in e for e in processed.errors)

    def test_what_it_changes_is_marked_derived(self):
        processed = process(_definition(), {"total": "4.50", "vendor": "LUNA"})
        assert processed.data["total"] == Decimal("9.00")
        assert processed.derived == ["total"]
        assert processed.transformed == {"total": "9.00", "vendor": "LUNA"}

    def test_a_reformatted_amount_is_not_a_change(self):
        """`4.5` and `4.50` are the same money; that is not deriving anything."""
        code = "def transform(data):\n    return {**data, 'total': '4.5'}\n"
        assert process(_definition(code), {"total": "4.50"}).derived == []

    def test_its_output_faces_the_same_rules(self):
        code = "def transform(data):\n    return {**data, 'total': '-3.00'}\n"
        processed = process(_definition(code), {"total": "3.00"})
        assert any("must be greater than 0" in e for e in processed.errors)

    def test_an_output_of_the_wrong_shape_is_reported_as_the_transforms(self):
        code = "def transform(data):\n    return {**data, 'total': 'lots'}\n"
        processed = process(_definition(code), {"total": "3.00"})
        assert any(e.startswith("transform returned total") for e in processed.errors)

    def test_keys_the_type_does_not_declare_are_dropped(self):
        code = "def transform(data):\n    return {**data, 'surprise': 1}\n"
        assert "surprise" not in process(_definition(code), {"total": "3.00"}).data

    def test_a_failing_transform_keeps_the_models_reading_and_says_why(self):
        code = "def transform(data):\n    raise ValueError('no idea')\n"
        processed = process(_definition(code), {"total": "3.00"})
        assert processed.data["total"] == Decimal("3.00")
        assert processed.transformed is None
        assert "transform failed: ValueError: no idea (line 2)" in processed.errors


class TestExamples:
    def test_passing_examples_report_nothing(self):
        d = _definition(examples=[{"input": {"total": "2.00"}, "expected": {"total": "4"}}])
        assert check_examples(d) == []

    def test_a_wrong_answer_is_reported(self):
        d = _definition(examples=[{"input": {"total": "2.00"}, "expected": {"total": "5.00"}}])
        assert check_examples(d) == ["example 1: total should be '5.00', got '4.00'"]

    def test_a_crash_is_reported(self):
        d = _definition(examples=[{"input": {}, "expected": {"total": "1.00"}}])
        assert "example 1: TypeError" in check_examples(d)[0]

    def test_seeding_refuses_a_transform_that_fails_its_examples(self, session):
        d = _definition(examples=[{"input": {"total": "2.00"}, "expected": {"total": "5.00"}}])
        with pytest.raises(ValueError, match="fails its examples"):
            seed.sync(session, [d])
        assert session.query(DocumentTypeDefinition).count() == 0

    def test_the_shipped_transforms_pass_their_examples(self):
        for definition in seed.load_seeds():
            assert check_examples(definition) == [], definition.name


class TestAttachingAFile:
    def _write(self, directory, transform):
        spec = {
            "name": "receipt",
            "matches": {"keywords": ["total"]},
            "fields": {"total": {"type": "money", "description": "The total."}},
            "transform": transform,
        }
        (directory / "receipt.yaml").write_text(yaml.safe_dump(spec))

    def test_the_file_is_read_into_the_definition(self, tmp_path):
        (tmp_path / "receipt_transform.py").write_text(DOUBLE)
        self._write(tmp_path, {"file": "receipt_transform.py"})

        [definition] = seed.load_seeds(tmp_path)
        assert definition.transform.code == DOUBLE

    def test_a_file_outside_the_seed_directory_is_refused(self, tmp_path):
        inner = tmp_path / "seeds"
        inner.mkdir()
        (tmp_path / "elsewhere.py").write_text(DOUBLE)
        self._write(inner, {"file": "../elsewhere.py"})
        with pytest.raises(ValueError, match="beside the seed"):
            seed.load_seeds(inner)

    def test_code_and_file_together_are_refused(self, tmp_path):
        (tmp_path / "t.py").write_text(DOUBLE)
        self._write(tmp_path, {"file": "t.py", "code": DOUBLE})
        with pytest.raises(ValueError, match="not both"):
            seed.load_seeds(tmp_path)


class TestTheInvoiceTransform:
    """The rules agreed for missing totals, as they run on a real document type."""

    @staticmethod
    def _invoice() -> Definition:
        return next(d for d in seed.load_seeds() if d.name == "invoice")

    def test_a_missing_total_is_derived_from_subtotal_and_tax(self):
        processed = process(self._invoice(), {"subtotal": "100.00", "tax_amount": "20.00"})
        assert processed.data["total"] == Decimal("120.00")
        assert processed.derived == ["total"]
        assert processed.errors == []

    def test_a_printed_total_is_never_touched(self):
        processed = process(self._invoice(), {"subtotal": "100.00", "total": "99.00"})
        assert processed.data["total"] == Decimal("99.00")
        assert processed.derived == []

    def test_no_subtotal_means_nothing_is_derived(self):
        processed = process(self._invoice(), {"tax_amount": "20.00"})
        assert processed.data["total"] is None
        assert any("total" in e for e in processed.errors)  # the required rule

    def test_the_ledger_marks_the_amount_as_derived(self):
        definition = self._invoice()
        processed = process(definition, {"subtotal": "100.00", "tax_amount": "20.00"})
        assert derived_columns(definition, processed.data, processed.derived) == ["amount"]

    def test_derived_tax_lines_mean_a_derived_ledger_tax(self):
        definition = self._invoice()
        data = {"tax_amount": None}
        assert derived_columns(definition, data, ["taxes"]) == ["tax_amount"]
        assert derived_columns(definition, {"tax_amount": Decimal("1")}, ["taxes"]) == []


class TestInTheWorker:
    TEXT = "ACME LTD\nINVOICE INV-2041\nDue Date: 13 April 2026\nSubtotal 100.00\nVAT 20.00"

    @pytest.fixture(autouse=True)
    def _cold_registry(self):
        registry.clear_cache()
        yield
        registry.clear_cache()

    def _run(self, session, data):
        from worker import run as worker

        seed.sync(session, seed.load_seeds())
        document, _ = repository.ingest(
            session,
            organisation_id=demo_org_id(session),
            data=TINY_PNG, mime_type="image/png", original_filename="i.png"
        )
        document.status = DocStatus.OCR_DONE
        session.add(OcrResult(
            document_id=document.id, engine="fake", engine_version="1", params={},
            full_text=self.TEXT, layout_blocks=[], mean_confidence=None,
        ))
        job = Job(kind=JobKind.EXTRACT, document_id=document.id)
        session.add(job)
        session.commit()
        worker.run_extract_job(session, worker.claim(session, me="test"), FakeExtractor(data))
        session.refresh(document)
        return document

    def test_a_derived_total_reaches_the_ledger_marked_as_derived(self, session):
        document = self._run(session, {
            "vendor": "ACME LTD", "currency": "GBP",
            "subtotal": "100.00", "tax_amount": "20.00", "total": None,
        })

        assert document.status == DocStatus.COMMITTED
        extraction = session.query(Extraction).one()
        assert extraction.raw_json["total"] is None  # what the model read, untouched
        assert extraction.transformed_json["total"] == "120.00"
        assert extraction.derived_fields == ["total"]
        row = session.query(Transaction).one()
        assert row.amount == Decimal("120.00")
        assert row.derived_fields == ["amount"]

    def test_a_printed_total_reaches_the_ledger_with_nothing_derived(self, session):
        self._run(session, {"subtotal": "100.00", "tax_amount": "20.00", "total": "120.00"})

        assert session.query(Extraction).one().derived_fields == []
        assert session.query(Transaction).one().derived_fields == []
