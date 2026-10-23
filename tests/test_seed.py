"""
The shipped seed document types.

Two things are being checked here. That the loader is idempotent, because it
runs on every deploy. And that `receipt.yaml` is actually fit for purpose --
it is prose in a file, so nothing else would notice it rotting.
"""

from __future__ import annotations

import pytest
import yaml

from core import repository
from core.models import DocumentTypeDefinition
from doctypes import registry, seed
from doctypes.definition import Definition
from doctypes.schema import build_json_schema, match_score
from doctypes.validate import validate

# A realistic Spanish till roll, of the kind the seed is written against.
RECEIPT_TEXT = """CAFETERIA LA LUNA
C/ Mayor 14, 28013 Madrid
CIF: A46103834
TICKET 00241        12/05/2026  13:42
2 x Cafe con leche        3,60
Tostada tomate            2,50
Zumo naranja              2,20
BASE IMPONIBLE            7,55
IVA 10%                   0,75
TOTAL                     8,30
TARJETA                   8,30
Gracias por su compra
"""


@pytest.fixture(autouse=True)
def _cold_registry():
    registry.clear_cache()
    yield
    registry.clear_cache()


class TestTheShippedSeeds:
    def test_every_seed_file_parses(self):
        """A malformed seed should fail here, not on the first live document."""
        definitions = seed.load_seeds()
        assert definitions, "no seed files found"
        assert all(isinstance(d, Definition) for d in definitions)

    def test_receipt_is_one_of_them(self):
        assert "receipt" in {d.name for d in seed.load_seeds()}

    def test_a_malformed_seed_names_the_file(self, tmp_path):
        (tmp_path / "broken.yaml").write_text(
            yaml.safe_dump({"name": "broken", "fields": {}})
        )
        with pytest.raises(ValueError, match="broken.yaml"):
            seed.load_seeds(tmp_path)

    def test_an_empty_directory_is_not_an_error(self, tmp_path):
        assert seed.load_seeds(tmp_path) == []


class TestTheReceiptTypeWorks:
    """The seed is prose; these are the assertions that keep it honest."""

    @staticmethod
    def _receipt() -> Definition:
        return next(d for d in seed.load_seeds() if d.name == "receipt")

    def test_it_matches_a_real_till_roll(self):
        assert match_score(self._receipt(), RECEIPT_TEXT) > 0

    def test_it_does_not_claim_an_unrelated_document(self):
        letter = "Dear Sir,\nPlease find enclosed the signed lease agreement.\nRegards,\n"
        assert match_score(self._receipt(), letter) == 0.0

    def test_one_stray_keyword_is_not_enough(self):
        """`total` alone appears on half the documents in the world."""
        assert match_score(self._receipt(), "Total pages: 4") == 0.0

    def test_its_schema_builds_and_carries_the_descriptions(self):
        schema = build_json_schema(self._receipt())
        assert schema["additionalProperties"] is False
        assert set(schema["required"]) == set(self._receipt().fields)
        # The description is the instruction, so it has to survive the trip.
        assert "TOTAL" in schema["properties"]["total"]["description"]

    def test_a_good_extraction_passes_every_rule(self):
        """The numbers below are what the seed's own example receipt says."""
        _, errors = validate(
            self._receipt(),
            {
                "vendor": "CAFETERIA LA LUNA",
                "tax_id": "A46103834",
                "issued_on": "2026-05-12",
                "subtotal": "7,55",
                "tax_amount": "0,75",
                "total": "8,30",
                "currency": "EUR",
                "payment_method": "card",
                "line_items": [
                    {"name": "Cafe con leche", "quantity": 2, "amount": "3,60"},
                    {"name": "Tostada tomate", "quantity": None, "amount": "2,50"},
                    {"name": "Zumo naranja", "quantity": None, "amount": "2,20"},
                ],
            },
        )
        assert errors == []

    def test_a_sparse_till_roll_still_passes(self):
        """Most of the fields are optional, and the rules must not punish that."""
        _, errors = validate(self._receipt(), {"total": "8,30"})
        assert errors == []

    def test_a_total_that_does_not_match_base_plus_tax_is_caught(self):
        _, errors = validate(
            self._receipt(),
            {"subtotal": "7,55", "tax_amount": "0,75", "total": "9,99"},
        )
        assert len(errors) == 1
        assert "IVA" in errors[0]

    def test_a_missing_total_is_caught(self):
        _, errors = validate(self._receipt(), {"vendor": "X"})
        assert any("total" in e for e in errors)


class TestSync:
    def test_it_creates_the_types_on_an_empty_table(self, session):
        actions = seed.sync(session, seed.load_seeds())

        assert actions["receipt"] == "created (v1)"
        row = repository.get_active_document_type(session, "receipt")
        assert row is not None
        assert row.is_active is True
        assert row.is_published is True

    def test_running_twice_writes_nothing_the_second_time(self, session):
        definitions = seed.load_seeds()
        seed.sync(session, definitions)
        actions = seed.sync(session, definitions)

        assert actions["receipt"] == "unchanged (v1)"
        assert session.query(DocumentTypeDefinition).count() == len(definitions)

    def test_an_edited_seed_writes_a_new_version(self, session):
        definitions = seed.load_seeds()
        seed.sync(session, definitions)

        edited = [
            d.model_copy(update={"display_name": "Till receipt"})
            if d.name == "receipt"
            else d
            for d in definitions
        ]
        actions = seed.sync(session, edited)

        assert actions["receipt"] == "updated (v2)"
        active = repository.get_active_document_type(session, "receipt")
        assert active.version == 2
        assert active.display_name == "Till receipt"
        # The old version is kept, because extractions reference it.
        assert session.query(DocumentTypeDefinition).filter_by(name="receipt").count() == 2

    def test_only_one_version_is_ever_active(self, session):
        definitions = seed.load_seeds()
        seed.sync(session, definitions)
        for i in range(3):
            seed.sync(
                session,
                [d.model_copy(update={"description": f"revision {i}"}) for d in definitions],
            )

        active = (
            session.query(DocumentTypeDefinition)
            .filter_by(name="receipt", is_active=True)
            .all()
        )
        assert len(active) == 1

    def test_a_version_bump_alone_is_not_a_content_change(self, session):
        """The file does not carry a trustworthy version; content decides."""
        definitions = seed.load_seeds()
        seed.sync(session, definitions)

        bumped = [d.model_copy(update={"version": 99}) for d in definitions]
        assert seed.sync(session, bumped)["receipt"] == "unchanged (v1)"


class TestSeededRoutingWorks:
    def test_a_seeded_receipt_is_routable(self, session):
        """The point of the whole exercise: a fresh database routes a receipt."""
        seed.sync(session, seed.load_seeds())

        match = registry.route(session, RECEIPT_TEXT)

        assert match is not None
        assert match.definition.name == "receipt"
        assert match.score > 0
        assert match.profile_key is None  # no issuer profiles shipped yet

    def test_an_unseeded_database_routes_nothing(self, session):
        assert registry.route(session, RECEIPT_TEXT) is None


class TestTheInvoiceType:
    """Two types registered means routing has to choose; these pin the choice."""

    INVOICE_TEXT = (
        "ACME SUPPLIES LTD\nINVOICE INV-2041\nDate: 14 March 2026\nDue Date: 13 April 2026\n"
        "Bill To: Northwind Traders\nToner cartridge 89.99\nSubtotal 119.49\nVAT 20% 23.90\n"
        "Total due 143.39"
    )

    @staticmethod
    def _types():
        return {d.name: d for d in seed.load_seeds()}

    def test_an_english_invoice_routes_to_invoice_not_receipt(self):
        types = self._types()
        assert match_score(types["invoice"], self.INVOICE_TEXT) > 0
        assert match_score(types["receipt"], self.INVOICE_TEXT) == 0.0

    def test_a_spanish_receipt_routes_to_receipt_not_invoice(self):
        types = self._types()
        assert match_score(types["receipt"], RECEIPT_TEXT) > 0
        assert match_score(types["invoice"], RECEIPT_TEXT) == 0.0

    def test_a_customer_called_sullivan_does_not_make_it_a_receipt(self):
        """
        The holdout failure: `iva` matched inside the name, and receipt's 2 of
        8 outscored invoice's 2 of 9. Text shaped like FATURA template 22.
        """
        text = (
            "INVOICE\nCustomer: Mark Sullivan\nsullivanmark@example.com\n"
            "Due Date: 02-Mar-2021\nWidget 2 40.00\nTOTAL 80.00"
        )
        picked = registry.pick(sorted(self._types().values(), key=lambda d: d.name), text)
        assert picked is not None
        assert picked[0].name == "invoice"

    def test_an_italian_receipt_still_routes_to_receipt(self):
        """It used to, only because `total` matched inside TOTALE."""
        text = "SUPERMERCATO\nDOCUMENTO COMMERCIALE\nPANE 1,20\nTOTALE COMPLESSIVO 1,20\ndi cui IVA 0,05"
        picked = registry.pick(list(self._types().values()), text)
        assert picked is not None
        assert picked[0].name == "receipt"

    def test_total_alone_claims_nothing(self):
        """`total` is on every financial document; it identifies none of them."""
        assert match_score(self._types()["invoice"], "TOTAL 9.35") == 0.0

    def test_a_good_extraction_passes_its_rules(self):
        _, errors = validate(
            self._types()["invoice"],
            {"invoice_number": "INV-2041", "total": "143.39", "currency": "GBP",
             "line_items": [{"description": "Toner", "quantity": 1, "unit_price": "89.99",
                             "amount": None}]},
        )
        assert errors == []

    def test_a_logo_only_seller_can_be_null(self):
        _, errors = validate(self._types()["invoice"], {"total": "10.00", "vendor": None})
        assert errors == []

    def test_it_reaches_the_ledger(self):
        from doctypes.canonical import to_ledger
        from doctypes.validate import coerce

        definition = self._types()["invoice"]
        data, _ = coerce(definition, {"total": "143.39", "vendor": "ACME", "currency": "GBP"})
        values, reason = to_ledger(definition, data)
        assert reason is None
        assert values["direction"] == "out"
        assert values["counterparty"] == "ACME"


def test_no_shipped_type_forces_a_document_level_amount():
    """
    A `required: true` field is non-nullable in the schema, so structured
    outputs force an answer even when the document prints nothing -- and the
    model computes one. Three FATURA invoices with no total line came back
    with the line items summed, two of them with a stray minus sign. Absence
    is the `required` rule's job: it sends the document to review instead.
    """
    from doctypes.definition import FieldType

    for definition in seed.load_seeds():
        forced = [
            name
            for name, spec in definition.fields.items()
            if spec.type is FieldType.MONEY and spec.required
        ]
        assert forced == [], f"{definition.name}: {forced} would be invented when absent"
        assert any(r.type == "required" and r.field == "total" for r in definition.rules)


class TestTheNullableFieldCap:
    """
    Structured outputs refuse a schema with more than 16 union-typed
    parameters. The invoice type reached 18 when `taxes` was added, and the
    API rejected every request; this is now caught when a type is defined.
    """

    def test_the_counter_matches_the_real_schema(self):
        import json

        from doctypes.definition import _count_nullable
        from doctypes.schema import build_json_schema

        for d in seed.load_seeds():
            assert _count_nullable(d.fields) == json.dumps(build_json_schema(d)).count("anyOf")

    def test_every_shipped_type_is_within_the_cap(self):
        from doctypes.definition import MAX_NULLABLE_FIELDS, _count_nullable

        for d in seed.load_seeds():
            assert _count_nullable(d.fields) <= MAX_NULLABLE_FIELDS, d.name

    def test_a_type_over_the_cap_is_refused_when_defined(self):
        from pydantic import ValidationError

        fields = {f"f{i}": {"type": "string", "description": "x"} for i in range(17)}
        with pytest.raises(ValidationError, match="at most 16"):
            Definition.model_validate(
                {"name": "wide", "matches": {"keywords": ["x"]}, "fields": fields}
            )

    def test_required_and_list_fields_do_not_count(self):
        from doctypes.definition import _count_nullable

        fields = Definition.model_validate({
            "name": "t", "matches": {"keywords": ["x"]},
            "fields": {
                "a": {"type": "string", "description": "x", "required": True},
                "items": {"type": "list", "description": "x", "item_fields": {
                    "b": {"type": "string", "description": "x"},
                    "c": {"type": "string", "description": "x", "required": True},
                }},
            },
        }).fields
        assert _count_nullable(fields) == 1


def test_a_stored_definition_that_breaks_a_new_rule_is_superseded(session):
    """
    Raising the bar on definitions can make a stored one invalid. The registry
    skips it, so the type stops routing; the seed must replace it, not crash.
    """
    seed.sync(session, seed.load_seeds())
    row = repository.get_active_document_type(session, "invoice")
    broken = dict(row.definition)
    broken["fields"] = {
        **broken["fields"],
        **{f"extra{i}": {"type": "string", "description": "x"} for i in range(5)},
    }
    row.definition = broken
    session.commit()

    actions = seed.sync(session, seed.load_seeds())

    assert actions["invoice"].startswith("updated")
