"""
Turning a definition into what the model sees: a JSON Schema and a score.

These two functions decide what gets asked for and which type gets to ask, so
they are the pair most able to go wrong quietly -- a schema the API rejects
fails loudly, but a schema that asks for the wrong shape just returns worse
data.
"""

from __future__ import annotations

from doctypes.definition import Definition
from doctypes.schema import build_json_schema, match_score, select_profile


def _definition(**overrides) -> Definition:
    spec = {
        "name": "receipt",
        "matches": {"keywords": ["total"], "min_score": 0.3},
        "fields": {
            "total": {"type": "money", "description": "The total.", "required": True},
            "vendor": {"type": "string", "description": "The vendor."},
        },
    }
    spec.update(overrides)
    return Definition.model_validate(spec)


class TestTopLevelFields:
    def test_every_field_is_required_so_no_key_is_ever_missing(self):
        schema = build_json_schema(_definition())
        assert set(schema["required"]) == {"total", "vendor"}

    def test_an_optional_field_is_nullable_instead(self):
        props = build_json_schema(_definition())["properties"]
        assert props["vendor"]["anyOf"] == [{"type": "string"}, {"type": "null"}]

    def test_a_required_field_is_not_wrapped(self):
        props = build_json_schema(_definition())["properties"]
        assert "anyOf" not in props["total"]
        assert props["total"]["type"] == "string"  # money crosses the wire as a string

    def test_additional_properties_are_refused(self):
        assert build_json_schema(_definition())["additionalProperties"] is False

    def test_the_description_survives_on_both_shapes(self):
        """The description is the instruction, so it has to be on the property."""
        props = build_json_schema(_definition())["properties"]
        assert props["total"]["description"] == "The total."
        assert props["vendor"]["description"] == "The vendor."

    def test_choices_become_an_enum(self):
        d = _definition(
            fields={
                "currency": {
                    "type": "string",
                    "description": "The currency.",
                    "required": True,
                    "choices": ["EUR", "USD"],
                }
            }
        )
        assert build_json_schema(d)["properties"]["currency"]["enum"] == ["EUR", "USD"]

    def test_money_carries_a_pattern_so_a_float_cannot_come_back(self):
        props = build_json_schema(_definition())["properties"]
        assert "pattern" in props["total"]


class TestListItemFields:
    """
    The rule that applies at the top level has to apply inside `items` too.
    It did not, once: an optional item field was merely absent from the
    entry's `required` list, so the model omitted the key entirely while every
    optional top-level field came back as an explicit null.
    """

    definition = _definition(
        fields={
            "line_items": {
                "type": "list",
                "description": "The items.",
                "item_fields": {
                    "name": {"type": "string", "description": "Name.", "required": True},
                    "qty": {"type": "number", "description": "Quantity."},
                    "amount": {"type": "money", "description": "Line total.", "required": True},
                },
            }
        }
    )

    @property
    def items(self) -> dict:
        return build_json_schema(self.definition)["properties"]["line_items"]["items"]

    def test_every_item_key_is_required(self):
        assert set(self.items["required"]) == {"name", "qty", "amount"}

    def test_an_optional_item_field_is_nullable(self):
        assert self.items["properties"]["qty"]["anyOf"] == [
            {"type": "number"},
            {"type": "null"},
        ]

    def test_a_required_item_field_is_not_wrapped(self):
        assert "anyOf" not in self.items["properties"]["name"]

    def test_item_objects_also_refuse_extra_keys(self):
        assert self.items["additionalProperties"] is False

    def test_a_list_itself_is_never_nullable(self):
        """"No items" is `[]`; nothing downstream wants to tell that from null."""
        prop = build_json_schema(self.definition)["properties"]["line_items"]
        assert prop["type"] == "array"
        assert "anyOf" not in prop

    def test_item_descriptions_survive(self):
        assert self.items["properties"]["qty"]["description"] == "Quantity."


class TestProfileOverrides:
    definition = _definition(
        profiles={
            "luna": {
                "tax_ids": ["A46103834"],
                "descriptions": {"total": "The figure below the card line."},
            }
        }
    )

    def test_an_override_replaces_the_description(self):
        _, profile = select_profile(self.definition, "CIF A46103834")
        props = build_json_schema(self.definition, profile)["properties"]
        assert props["total"]["description"] == "The figure below the card line."

    def test_unlisted_fields_keep_their_default(self):
        _, profile = select_profile(self.definition, "CIF A46103834")
        props = build_json_schema(self.definition, profile)["properties"]
        assert props["vendor"]["description"] == "The vendor."

    def test_a_profile_cannot_change_the_field_set(self):
        """The shape belongs to the type; only the layout belongs to the issuer."""
        _, profile = select_profile(self.definition, "CIF A46103834")
        with_profile = build_json_schema(self.definition, profile)
        without = build_json_schema(self.definition)
        assert with_profile["required"] == without["required"]
        assert with_profile["properties"].keys() == without["properties"].keys()

    def test_no_profile_matches_an_unknown_issuer(self):
        assert select_profile(self.definition, "SOME OTHER SHOP") == (None, None)

    def test_a_tax_id_is_preferred_over_a_name(self):
        """Names vary ("BBVA", "BBVA S.A."); a tax id does not."""
        d = _definition(
            profiles={
                "by_name": {"name_patterns": ["LUNA"], "descriptions": {}},
                "by_tax_id": {"tax_ids": ["A46103834"], "descriptions": {}},
            }
        )
        key, _ = select_profile(d, "CAFE LUNA  CIF A46103834")
        assert key == "by_tax_id"


class TestMatchScore:
    def test_a_matching_document_scores_above_zero(self):
        assert match_score(_definition(), "TOTAL 9.35") > 0

    def test_an_unrelated_document_scores_zero(self):
        assert match_score(_definition(), "Dear Sir, please find enclosed") == 0.0

    def test_scoring_ignores_case_and_stretched_whitespace(self):
        """A stretched till roll prints T O T A L; it is still the word."""
        assert match_score(_definition(), "t o t a l") == 0.0  # letters, not the word
        assert match_score(_definition(), "Total") > 0

    def test_below_min_score_is_reported_as_no_match_at_all(self):
        d = _definition(
            matches={"keywords": ["total", "iva", "ticket", "gracias"], "min_score": 0.75}
        )
        assert match_score(d, "TOTAL 9.35") == 0.0  # 1 of 4 signals

    def test_require_all_gates_rather_than_contributes(self):
        d = _definition(
            matches={"keywords": ["total"], "require_all": ["factura"], "min_score": 0.1}
        )
        assert match_score(d, "TOTAL 9.35") == 0.0
        assert match_score(d, "FACTURA  TOTAL 9.35") > 0

    def test_a_keyword_inside_a_longer_word_does_not_count(self):
        """`iva` in "Sullivan" sent two English invoices to the receipt type."""
        d = _definition(matches={"keywords": ["iva"], "min_score": 0.1})
        assert match_score(d, "Bill To: Mark Sullivan") == 0.0
        assert match_score(d, "rivassarah@example.com") == 0.0
        assert match_score(d, "IVA 21%") > 0

    def test_punctuation_and_glued_digits_still_bound_a_word(self):
        d = _definition(matches={"keywords": ["iva"], "min_score": 0.1})
        assert match_score(d, "IVA:") > 0
        assert match_score(d, "IVA21%") > 0

    def test_a_phrase_matches_across_a_line_break(self):
        d = _definition(matches={"keywords": ["bill to"], "min_score": 0.1})
        assert match_score(d, "BILL\nTO Northwind") > 0

    def test_require_all_also_matches_whole_words(self):
        d = _definition(
            matches={"keywords": ["total"], "require_all": ["factura"], "min_score": 0.1}
        )
        assert match_score(d, "FACTURAS  TOTAL 9.35") == 0.0

    def test_a_pattern_counts_as_a_signal(self):
        d = _definition(
            matches={"keywords": ["total"], "patterns": [r"\b[A-Z]\d{8}\b"], "min_score": 0.9}
        )
        assert match_score(d, "TOTAL 9.35") == 0.0  # 1 of 2
        assert match_score(d, "TOTAL 9.35 A46103834") == 1.0  # 2 of 2
