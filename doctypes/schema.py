"""
Turning a definition into the two things the model actually sees: a JSON Schema
and a routing score.

The schema carries the field descriptions, and those descriptions are the
extraction instructions -- Pydantic and the API pass `description` through
verbatim. That is why a per-issuer profile only needs to override descriptions
to change extraction behaviour, and why it must not change the field set.
"""

from __future__ import annotations

import re

from doctypes.definition import Definition, FieldSpec, FieldType, Profile

# JSON Schema fragment per field type. `money` is a string rather than a number
# so the value survives the wire without becoming a float -- it is parsed to
# Decimal on the way in.
_JSON_TYPE: dict[FieldType, dict] = {
    FieldType.STRING: {"type": "string"},
    FieldType.INTEGER: {"type": "integer"},
    FieldType.NUMBER: {"type": "number"},
    FieldType.MONEY: {"type": "string", "pattern": r"^-?\d+([.,]\d{1,2})?$"},
    FieldType.DATE: {"type": "string", "format": "date"},
    FieldType.BOOLEAN: {"type": "boolean"},
}


def _field_schema(spec: FieldSpec, description: str) -> dict:
    """The bare fragment for a field, before the optional-means-nullable rule."""
    if spec.type is FieldType.LIST:
        assert spec.item_fields is not None  # guaranteed by Definition
        return {
            "type": "array",
            "description": description,
            "items": {
                "type": "object",
                "properties": {
                    name: _property_schema(item, item.description)
                    for name, item in spec.item_fields.items()
                },
                # Every item key present, optional ones nullable -- the same
                # rule as the top level, for the same reason.
                "required": list(spec.item_fields),
                "additionalProperties": False,
            },
        }

    fragment = dict(_JSON_TYPE[spec.type])
    fragment["description"] = description
    if spec.choices:
        fragment["enum"] = list(spec.choices)
    return fragment


def _property_schema(spec: FieldSpec, description: str) -> dict:
    """
    One property, with the optional-means-nullable rule applied.

    The rule lives here rather than at the call site so that it reaches list
    item fields as well as top-level ones. It did not, once: an optional item
    field was merely absent from the entry's `required` list, so the model was
    free to omit the key entirely -- and did -- while every optional top-level
    field came back as an explicit null. Two shapes for one idea, and the
    difference only showed up in a live call.

    Lists are exempt, at either level: "no items" is `[]`, and nothing
    downstream wants to tell that apart from null.
    """
    fragment = _field_schema(spec, description)
    if spec.required or spec.type is FieldType.LIST:
        return fragment

    # The description goes on the property, not on both branches of the
    # anyOf. A duplicate says nothing new and is paid for on every call --
    # field descriptions are already most of the input tokens.
    inner = {k: v for k, v in fragment.items() if k != "description"}
    return {"anyOf": [inner, {"type": "null"}], "description": description}


def build_json_schema(definition: Definition, profile: Profile | None = None) -> dict:
    """
    The schema sent to the model, with this issuer's description overrides
    applied.

    Optional fields are nullable rather than absent from `required`: structured
    outputs want every key present, and an explicit null is a clearer signal
    than a missing key ("I looked and it was not there" vs "I forgot"). That
    applies inside list items too -- see `_property_schema`.
    """
    overrides = profile.descriptions if profile else {}
    properties: dict[str, dict] = {}

    for name, spec in definition.fields.items():
        description = overrides.get(name, spec.description)
        properties[name] = _property_schema(spec, description)

    return {
        "type": "object",
        "title": definition.name,
        "properties": properties,
        # Every key present; optional ones may be null. additionalProperties
        # false is required by strict structured outputs.
        "required": list(definition.fields),
        "additionalProperties": False,
    }


def select_profile(definition: Definition, ocr_text: str) -> tuple[str | None, Profile | None]:
    """
    Find the issuer profile for this document, if one is registered.

    Tax ids are checked before names because they are stable -- a trading name
    appears as "BBVA", "BBVA S.A." and "Banco Bilbao Vizcaya" on documents from
    the same issuer.
    """
    haystack = _normalise(ocr_text)

    for key, profile in definition.profiles.items():
        if any(_normalise(t) in haystack for t in profile.tax_ids):
            return key, profile

    for key, profile in definition.profiles.items():
        if any(re.search(p, ocr_text, re.IGNORECASE) for p in profile.name_patterns):
            return key, profile

    return None, None


def match_score(definition: Definition, ocr_text: str) -> float:
    """
    How strongly this text looks like this document type, 0.0 to 1.0.

    Deterministic and free, so every registered type can be scored on every
    document and the highest scorer above its own `min_score` wins. A model
    classifier is only needed when nothing matches.

    `require_all` is a gate rather than a contributor: if any of those terms is
    missing the answer is zero, however many other keywords hit.
    """
    matchers = definition.matches
    haystack = _normalise(ocr_text)

    if matchers.require_all and not all(
        _normalise(term) in haystack for term in matchers.require_all
    ):
        return 0.0

    signals = len(matchers.keywords) + len(matchers.patterns)
    if signals == 0:
        # Only require_all was declared, and it passed.
        return 1.0 if matchers.require_all else 0.0

    hits = sum(1 for k in matchers.keywords if _normalise(k) in haystack)
    hits += sum(1 for p in matchers.patterns if re.search(p, ocr_text, re.IGNORECASE))

    score = hits / signals
    return score if score >= matchers.min_score else 0.0


def _normalise(text: str) -> str:
    """
    Case- and whitespace-insensitive comparison.

    OCR output is noisy about spacing -- "T O T A L" and "TOTAL" are the same
    word on a stretched receipt -- so runs of whitespace collapse to one space.
    """
    return re.sub(r"\s+", " ", text).casefold()
