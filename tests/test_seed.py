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
