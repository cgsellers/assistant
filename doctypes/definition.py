"""
The document type definition format.

A document type is data, not code: field names with the descriptions that tell
the model where to look, deterministic matchers for routing, validation rules,
and optional per-issuer description overrides.

The format is itself a Pydantic model, which buys three things:

  * a malformed definition is rejected with a precise error rather than failing
    halfway through an extraction;
  * `Definition.model_json_schema()` describes the format, so the authoring UI
    can generate its form instead of hard-coding one;
  * the same validation runs whether a definition came from a seed file, the
    database, or an analyst's unsaved draft.
"""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# Field names become JSON keys and eventually column names, so keep them
# conservative rather than discovering the hard way what breaks.
FIELD_NAME = re.compile(r"^[a-z][a-z0-9_]{0,48}$")


class FieldType(StrEnum):
    """
    The value kinds a definition may declare.

    Deliberately small. Each maps to a JSON Schema fragment and a coercion
    rule, and every addition is a new thing the rule evaluator must understand.
    """

    STRING = "string"
    INTEGER = "integer"
    NUMBER = "number"
    MONEY = "money"        # coerced to Decimal, never float
    DATE = "date"
    BOOLEAN = "boolean"
    LIST = "list"          # repeated group, e.g. line items


class FieldSpec(BaseModel):
    """One field the model must extract."""

    model_config = ConfigDict(extra="forbid")

    type: FieldType = FieldType.STRING
    #: The extraction instruction. This is the single most important string in
    #: the system -- it is what the model actually reads. Prefer describing
    #: labels ("labelled VENCIMIENTO") over positions ("top right"), which
    #: break silently when an issuer redesigns their template.
    description: str = Field(min_length=1, max_length=2000)
    required: bool = False
    #: For LIST fields: the shape of each entry.
    item_fields: dict[str, FieldSpec] | None = None
    #: Optional closed set, rendered into the schema as an enum.
    choices: list[str] | None = None

    @model_validator(mode="after")
    def _check_list_shape(self) -> FieldSpec:
        if self.type is FieldType.LIST and not self.item_fields:
            raise ValueError("a list field must declare item_fields")
        if self.type is not FieldType.LIST and self.item_fields:
            raise ValueError("item_fields is only valid on a list field")
        return self


class Matchers(BaseModel):
    """
    Deterministic routing. No model call, so running every registered type
    against a document costs nothing.
    """

    model_config = ConfigDict(extra="forbid")

    #: Any of these appearing in the OCR text is evidence for this type.
    keywords: list[str] = Field(default_factory=list)
    #: All of these must appear. Use for types that are otherwise ambiguous.
    require_all: list[str] = Field(default_factory=list)
    #: Regexes, for structural signals like a tax id format.
    patterns: list[str] = Field(default_factory=list)
    #: Score below which this type is not considered a match at all.
    min_score: float = Field(default=0.35, ge=0.0, le=1.0)

    @field_validator("patterns")
    @classmethod
    def _compilable(cls, patterns: list[str]) -> list[str]:
        for p in patterns:
            try:
                re.compile(p)
            except re.error as exc:
                raise ValueError(f"invalid regex {p!r}: {exc}") from exc
        return patterns

    @model_validator(mode="after")
    def _needs_a_signal(self) -> Matchers:
        if not (self.keywords or self.require_all or self.patterns):
            raise ValueError("matchers need at least one keyword or pattern")
        return self


# --- Validation rules -------------------------------------------------------
# A small closed vocabulary rather than an expression language: an analyst
# picks from a fixed set in the UI, and every rule is independently testable.
# Types that genuinely need arbitrary logic get a named Python validator via
# `python_rules` instead.


class _Rule(BaseModel):
    model_config = ConfigDict(extra="forbid")
    #: Shown to the analyst and stored in validation_errors. Optional: each
    #: rule can describe itself.
    message: str | None = None


class RequiredRule(_Rule):
    type: Literal["required"] = "required"
    field: str


class PositiveRule(_Rule):
    type: Literal["positive"] = "positive"
    field: str


class RangeRule(_Rule):
    type: Literal["range"] = "range"
    field: str
    min: float | None = None
    max: float | None = None


class SumEqualsRule(_Rule):
    """`of` summed must equal `target`, within `tolerance`."""

    type: Literal["sum_equals"] = "sum_equals"
    target: str
    of: list[str] = Field(min_length=1)
    #: Cents of slack. Real documents round; demanding exactness fails good
    #: extractions.
    tolerance: float = 0.02


class LineItemsSumRule(_Rule):
    """Each entry's `item_field` summed must equal `target`."""

    type: Literal["line_items_sum"] = "line_items_sum"
    list_field: str
    item_field: str
    target: str
    tolerance: float = 0.02


class NotFutureRule(_Rule):
    type: Literal["not_future"] = "not_future"
    field: str
    #: Small allowance for timezone skew and clock drift.
    grace_days: int = 1


class RegexRule(_Rule):
    type: Literal["regex"] = "regex"
    field: str
    pattern: str

    @field_validator("pattern")
    @classmethod
    def _compilable(cls, pattern: str) -> str:
        try:
            re.compile(pattern)
        except re.error as exc:
            raise ValueError(f"invalid regex: {exc}") from exc
        return pattern


Rule = Annotated[
    RequiredRule
    | PositiveRule
    | RangeRule
    | SumEqualsRule
    | LineItemsSumRule
    | NotFutureRule
    | RegexRule,
    Field(discriminator="type"),
]


class Profile(BaseModel):
    """
    Per-issuer description overrides.

    Does not restate the schema and cannot add or remove fields -- the target
    shape is a property of the document type, not of who issued it. Only the
    hints change, because only the layout changes.

    Earned, not authored upfront: a profile adds a prompt-cache namespace, so
    it pays for itself only once an issuer recurs.
    """

    model_config = ConfigDict(extra="forbid")

    #: The reliable matcher. A tax id is stable; a trading name is not
    #: ("BBVA" / "BBVA S.A." / "Banco Bilbao Vizcaya").
    tax_ids: list[str] = Field(default_factory=list)
    name_patterns: list[str] = Field(default_factory=list)
    #: field name -> replacement description. Unlisted fields keep the default.
    descriptions: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _needs_a_signal(self) -> Profile:
        if not (self.tax_ids or self.name_patterns):
            raise ValueError("a profile needs tax_ids or name_patterns to match on")
        return self


class Definition(BaseModel):
    """A complete document type."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,48}$")
    version: int = Field(default=1, ge=1)
    display_name: str | None = None
    description: str | None = None

    matches: Matchers
    fields: dict[str, FieldSpec] = Field(min_length=1)
    rules: list[Rule] = Field(default_factory=list)
    profiles: dict[str, Profile] = Field(default_factory=dict)

    @field_validator("fields")
    @classmethod
    def _field_names_are_safe(cls, fields: dict[str, FieldSpec]) -> dict[str, FieldSpec]:
        for name in fields:
            if not FIELD_NAME.match(name):
                raise ValueError(
                    f"field name {name!r} must be lower_snake_case and start with a letter"
                )
        return fields

    @model_validator(mode="after")
    def _rules_and_profiles_reference_real_fields(self) -> Definition:
        """
        Catch typos at definition time rather than as a silent no-op on every
        document. A rule naming a field that does not exist would otherwise
        never fire and never complain.
        """
        known = set(self.fields)

        for rule in self.rules:
            referenced = {
                *([rule.field] if getattr(rule, "field", None) else []),
                *([rule.target] if getattr(rule, "target", None) else []),
                *getattr(rule, "of", []),
                *([rule.list_field] if getattr(rule, "list_field", None) else []),
            }
            unknown = referenced - known
            if unknown:
                raise ValueError(
                    f"rule {rule.type!r} references unknown field(s): {sorted(unknown)}"
                )
            list_field = getattr(rule, "list_field", None)
            if list_field:
                spec = self.fields[list_field]
                if spec.item_fields is None or rule.item_field not in spec.item_fields:
                    raise ValueError(
                        f"rule {rule.type!r} references unknown item field "
                        f"{rule.item_field!r} on {list_field!r}"
                    )

        for key, profile in self.profiles.items():
            unknown = set(profile.descriptions) - known
            if unknown:
                raise ValueError(
                    f"profile {key!r} overrides unknown field(s): {sorted(unknown)}"
                )

        return self
