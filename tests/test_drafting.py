"""
Drafting a processor with a model: what is taken from its answer, what is
overruled, and what happens when the answer does not validate.
"""

from __future__ import annotations

import copy
from decimal import Decimal

import pytest

from core.models import DocumentTypeDefinition
from doctypes import authoring, drafting, seed
from doctypes.authoring import AuthoringError
from llm.base import ExtractionError, ExtractionResult

GOOD = {
    "name": "purchase_order",
    "display_name": "Purchase order",
    "description": "An order a customer sends us.",
    "keywords": ["purchase order", "po number", "ship to", "order date"],
    "patterns": [r"\bPO-\d+"],
    "fields": [
        {"name": "po_number", "type": "string",
         "description": "Labelled PO Number. Null if not printed.",
         "can_be_missing": True, "choices": [], "item_fields": []},
        # The model says it cannot be missing; the top level is nullable regardless.
        {"name": "total", "type": "money", "description": "Labelled Total. Null if not printed.",
         "can_be_missing": False, "choices": [], "item_fields": []},
        {"name": "currency", "type": "string", "description": "The currency code.",
         "can_be_missing": True, "choices": ["GBP", "EUR"], "item_fields": []},
        {"name": "lines", "type": "list", "description": "One per ordered item.",
         "can_be_missing": True, "choices": [], "item_fields": [
             {"name": "item", "type": "string", "description": "The item.",
              "can_be_missing": False, "choices": []},
             {"name": "amount", "type": "money", "description": "The line amount.",
              "can_be_missing": True, "choices": []},
         ]},
    ],
    "must_be_present": ["total"],
    "ledger": {"direction": "in", "amount": "total", "occurred_on": "", "counterparty": "",
               "counterparty_tax_id": "", "tax_amount": "", "currency": "currency"},
    "notes": "Check the PO label against real samples.",
}


class FakeDrafter:
    """Answers each call with the next canned draft, and keeps what it was sent."""

    def __init__(self, *answers, raises=None):
        self.answers = list(answers)
        self.raises = raises
        self.calls: list[dict] = []

    def complete(self, **kwargs):
        self.calls.append(kwargs)
        if self.raises is not None:
            raise self.raises
        return ExtractionResult(
            data=self.answers.pop(0), model="claude-sonnet-5", input_tokens=2000,
            output_tokens=900, cache_read_tokens=0, cost_usd=Decimal("0.013"),
            latency_ms=20_000, stop_reason="end_turn", params={},
        )


def _draft(session, drafter, **kwargs):
    return drafting.draft(
        session, description=kwargs.pop("description", "Purchase orders; PO number and total."),
        created_by="user:ana (ai draft)", drafter=drafter, **kwargs,
    )


class TestAGoodDraft:
    def test_it_is_saved_as_a_draft_nothing_routes_to(self, session):
        outcome = _draft(session, FakeDrafter(GOOD))

        row = outcome.row
        assert (row.name, row.version, row.is_published, row.is_active) == (
            "purchase_order", 1, False, False,
        )
        assert row.created_by == "user:ana (ai draft)"
        assert outcome.notes == "Check the PO label against real samples."
        assert outcome.cost_usd == Decimal("0.013")

    def test_top_level_fields_are_never_forced(self, session):
        """A forced amount that isn't on the page gets invented: presence is a rule."""
        definition = authoring.parse(_draft(session, FakeDrafter(GOOD)).row.definition)
        assert definition.fields["total"].required is False
        assert [(r.type, r.field) for r in definition.rules] == [("required", "total")]

    def test_list_parts_keep_what_the_model_said(self, session):
        items = authoring.parse(_draft(session, FakeDrafter(GOOD)).row.definition).fields[
            "lines"
        ].item_fields
        assert items["item"].required is True
        assert items["amount"].required is False

    def test_choices_and_the_ledger_mapping_carry_over(self, session):
        definition = authoring.parse(_draft(session, FakeDrafter(GOOD)).row.definition)
        assert definition.fields["currency"].choices == ["GBP", "EUR"]
        assert definition.canonical.direction == "in"
        assert definition.canonical.amount == "total"
        assert definition.canonical.occurred_on is None

    def test_no_transform_is_ever_drafted(self, session):
        assert authoring.parse(_draft(session, FakeDrafter(GOOD)).row.definition).transform is None

    def test_a_non_transaction_gets_no_ledger_mapping(self, session):
        answer = copy.deepcopy(GOOD)
        answer["ledger"]["direction"] = "none"
        row = _draft(session, FakeDrafter(answer)).row
        assert authoring.parse(row.definition).canonical is None


class TestWhatTheModelIsTold:
    def test_the_sample_and_existing_processors_are_in_the_request(self, session):
        seed.sync(session, seed.load_seeds())
        drafter = FakeDrafter(GOOD)
        _draft(session, drafter, sample="PURCHASE ORDER PO-1234\nTotal 50.00")

        message = drafter.calls[0]["user"]
        assert "PURCHASE ORDER PO-1234" in message
        assert "invoice, receipt" in message
        assert drafter.calls[0]["json_schema"] is drafting.DRAFT_SCHEMA

    def test_a_long_sample_is_clipped(self, session):
        drafter = FakeDrafter(GOOD)
        _draft(session, drafter, sample="x" * (drafting.SAMPLE_CHARS + 500))
        assert "x" * (drafting.SAMPLE_CHARS + 1) not in drafter.calls[0]["user"]

    def test_an_empty_description_is_refused_before_any_call(self, session):
        drafter = FakeDrafter(GOOD)
        with pytest.raises(AuthoringError, match="describe the document"):
            _draft(session, drafter, description="  ")
        assert drafter.calls == []

    def test_the_draft_schema_has_no_nullable_values(self):
        """Structured outputs cap union types at 16; this schema uses none."""
        import json

        assert "anyOf" not in json.dumps(drafting.DRAFT_SCHEMA)


class TestRepair:
    def test_an_invalid_draft_is_repaired_once_with_its_problems(self, session):
        broken = copy.deepcopy(GOOD)
        broken["ledger"]["amount"] = "po_number"  # not a money field
        drafter = FakeDrafter(broken, GOOD)

        outcome = _draft(session, drafter)

        assert outcome.row is not None
        assert outcome.attempts == 2
        assert outcome.cost_usd == Decimal("0.026")
        repair = drafter.calls[1]["user"]
        assert "Your previous draft was" in repair
        assert "must point at a money field" in repair

    def test_still_invalid_after_repair_returns_the_yaml_and_saves_nothing(self, session):
        broken = copy.deepcopy(GOOD)
        broken["patterns"] = ["(unclosed"]
        outcome = _draft(session, FakeDrafter(broken, broken))

        assert outcome.row is None
        assert any("invalid regex" in p for p in outcome.problems)
        assert "name: purchase_order" in outcome.yaml
        assert session.query(DocumentTypeDefinition).count() == 0

    def test_a_taken_name_is_sent_back_for_repair(self, session):
        seed.sync(session, seed.load_seeds())
        clash = copy.deepcopy(GOOD)
        clash["name"] = "invoice"
        drafter = FakeDrafter(clash, GOOD)

        assert _draft(session, drafter).row.name == "purchase_order"
        assert "already taken" in drafter.calls[1]["user"]

    def test_two_fields_with_one_name_are_caught(self, session):
        twice = copy.deepcopy(GOOD)
        twice["fields"].append(copy.deepcopy(twice["fields"][0]))
        outcome = _draft(session, FakeDrafter(twice, twice))
        assert outcome.problems == ["fields: po_number appears twice"]

    def test_a_failed_model_call_is_an_explained_error(self, session):
        drafter = FakeDrafter(raises=ExtractionError("rate limited"))
        with pytest.raises(AuthoringError, match="the model call failed: rate limited"):
            _draft(session, drafter)
