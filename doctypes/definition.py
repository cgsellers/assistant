"""
The document type definition format.

A document type is data, not code: field names with the descriptions that tell
the model where to look, deterministic matchers for routing, validation rules,
and optional per-issuer description overrides. The one exception is an
optional `transform`, for arithmetic the model is told not to do.

The format is itself a Pydantic model, which buys three things:

  * a malformed definition is rejected with a precise error rather than failing
    halfway through an extraction;
  * `Definition.model_json_schema()` describes the format, so the authoring UI
    can generate its form instead of hard-coding one;
  * the same validation runs whether a definition came from a seed file, the
    database, or an analyst's unsaved draft.
"""

from __future__ import annotations

import ast
import re
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

#: Structured outputs refuse a schema with more than this many union-typed
#: parameters ("Schemas contains too many parameters with union types ...
#: limit: 16 parameters with unions"). Every optional field is one -- it is
#: rendered as `anyOf: [T, null]` -- so this caps the optional fields a type
#: can have. Checked when a definition is built, because the alternative is
#: an analyst authoring a type that looks fine and then has every single
#: document rejected by the API.
MAX_NULLABLE_FIELDS = 16

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
# Computing values rather than checking them is a `Transform`'s job.


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


class Direction(StrEnum):
    """Which way the money went, from this business's point of view."""

    OUT = "out"  # a purchase receipt, a supplier invoice
    IN = "in"  # a sales invoice, a refund


class CanonicalMap(BaseModel):
    """
    How this document type's fields become a row in `transactions`.

    Data rather than code, for the same reason the rest of a definition is: a
    mapper written per type is a module per type, which is the design 5.20
    rejected. An analyst adding a document type in the UI has to be able to
    say "the total is the amount" without shipping Python.

    Only the fields the canonical layer actually has. Everything else a
    document type collects stays in `extractions.raw_json`, which is the point
    of having a raw layer: the ledger is deliberately narrow, and a field is
    promoted into it only when something downstream must act on it.

    `direction` is a constant rather than a field reference because it is a
    property of the type, not of the document -- every purchase receipt is
    money leaving. A type that could be either way round wants two types.
    """

    model_config = ConfigDict(extra="forbid")

    direction: Direction
    #: The field holding the amount charged. Required: a ledger row without
    #: an amount is not a transaction.
    amount: str
    occurred_on: str | None = None
    counterparty: str | None = None
    counterparty_tax_id: str | None = None
    tax_amount: str | None = None
    #: A list field of individual taxes, each with an `amount`. Used when
    #: `tax_amount` came back null -- a document listing GST and VAT separately
    #: with no combined line. The ledger's tax is then their sum, computed here
    #: in code, so the model never has to add them up itself.
    tax_lines: str | None = None
    currency: str | None = None

    def referenced(self) -> dict[str, str]:
        """Canonical column -> document type field name, for the ones that are set."""
        return {
            column: field
            for column, field in (
                ("amount", self.amount),
                ("occurred_on", self.occurred_on),
                ("counterparty", self.counterparty),
                ("counterparty_tax_id", self.counterparty_tax_id),
                ("tax_amount", self.tax_amount),
                ("tax_lines", self.tax_lines),
                ("currency", self.currency),
            )
            if field is not None
        }


class TransformExample(BaseModel):
    """
    One worked case for a transform: given this extraction, these fields
    should come out. Run before a definition is stored, so a transform that
    breaks fails when it is saved rather than on a live document.
    """

    model_config = ConfigDict(extra="forbid")

    #: Extracted data as the transform receives it: money as strings, dates
    #: as YYYY-MM-DD.
    input: dict
    #: Only the fields listed are checked; the rest may be anything.
    expected: dict = Field(min_length=1)


class Transform(BaseModel):
    """
    Python that runs on an extraction after the model has read it.

    For what the model is told not to do -- arithmetic, mostly. The code must
    define `transform(data: dict) -> dict`. It is written by people, not
    generated, and runs in a separate interpreter with the standard library
    only and a time limit (doctypes.transform). Whatever it changes is
    recorded as derived, and its output is validated like the model's.
    """

    model_config = ConfigDict(extra="forbid")

    #: The source. A seed file may give `file: name.py` instead, which the
    #: loader reads into here -- the stored definition always carries the
    #: code itself, so a version is a version of the code too.
    code: str = Field(min_length=1, max_length=50_000)
    timeout_seconds: float = Field(default=2.0, gt=0, le=30)
    examples: list[TransformExample] = Field(default_factory=list)

    @field_validator("code")
    @classmethod
    def _defines_transform(cls, code: str) -> str:
        """Parsed, never executed: a definition is validated on every load."""
        try:
            tree = ast.parse(code)
        except SyntaxError as exc:
            raise ValueError(f"transform code does not parse: {exc}") from exc
        functions = [
            node for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "transform"
        ]
        if not functions:
            raise ValueError("transform code must define a top-level `transform(data)`")
        arguments = functions[-1].args
        if len(arguments.posonlyargs) + len(arguments.args) != 1:
            raise ValueError("`transform` must take exactly one argument, the data")
        return code


class Definition(BaseModel):
    """A complete document type."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,48}$")
    version: int = Field(default=1, ge=1)
    #: Its column is VARCHAR(200), which Postgres enforces.
    display_name: str | None = Field(default=None, max_length=200)
    description: str | None = None

    matches: Matchers
    fields: dict[str, FieldSpec] = Field(min_length=1)
    rules: list[Rule] = Field(default_factory=list)
    profiles: dict[str, Profile] = Field(default_factory=dict)
    #: How to write this type into `transactions`. Optional: a type without
    #: one is extracted and validated but never reaches the ledger, which is
    #: the right behaviour for a type an analyst is still shaping.
    canonical: CanonicalMap | None = None
    #: Code run on each extraction before validation and the ledger.
    transform: Transform | None = None

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
            if len(key) > 100:
                # Stored on every extraction that uses it, in VARCHAR(100).
                raise ValueError(f"profile name {key[:20]!r}... is longer than 100 characters")
            unknown = set(profile.descriptions) - known
            if unknown:
                raise ValueError(
                    f"profile {key!r} overrides unknown field(s): {sorted(unknown)}"
                )

        nullable = _count_nullable(self.fields)
        if nullable > MAX_NULLABLE_FIELDS:
            raise ValueError(
                f"{nullable} optional fields, but structured outputs accept at most "
                f"{MAX_NULLABLE_FIELDS}: each optional field is a nullable union in the "
                "schema. Mark fields that are always present when their parent exists "
                "as required (e.g. a tax line's amount), or split the type."
            )

        if self.canonical is not None:
            # The canonical layer is typed where the raw layer is not, so a
            # mapping that points at the wrong kind of field would fail at
            # insert time on some later document rather than here.
            expected = {
                "amount": FieldType.MONEY,
                "tax_amount": FieldType.MONEY,
                "occurred_on": FieldType.DATE,
                "tax_lines": FieldType.LIST,
            }
            for column, name in self.canonical.referenced().items():
                if name not in known:
                    raise ValueError(
                        f"canonical.{column} references unknown field {name!r}"
                    )
                wanted = expected.get(column)
                if wanted is not None and self.fields[name].type is not wanted:
                    raise ValueError(
                        f"canonical.{column} must point at a {wanted} field, but "
                        f"{name!r} is {self.fields[name].type}"
                    )
            if self.canonical.tax_lines:
                items = self.fields[self.canonical.tax_lines].item_fields or {}
                if items.get("amount") is None or items["amount"].type is not FieldType.MONEY:
                    raise ValueError(
                        "canonical.tax_lines must point at a list whose entries "
                        "have a money field called `amount`"
                    )

        return self


def _count_nullable(fields: dict[str, FieldSpec]) -> int:
    """
    How many union-typed parameters this field set becomes in the schema.

    Mirrors doctypes.schema._property_schema: a field is nullable unless it is
    required or a list, at every level. A test checks this count against the
    real schema, so the two cannot drift apart.
    """
    count = 0
    for spec in fields.values():
        if spec.type is FieldType.LIST:
            count += _count_nullable(spec.item_fields or {})
        elif not spec.required:
            count += 1
    return count
