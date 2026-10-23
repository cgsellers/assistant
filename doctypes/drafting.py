"""
Drafting a processor from a plain-language description.

An author says what the document is and what they need from it -- "supplier
invoices from UK vendors: invoice number, dates, VAT and the total" -- and
optionally shows a sample. A model drafts the processor; the author reviews,
trial-runs and publishes it. The first slice of the template agent (roadmap
step 5), without its automatic trial-and-revise loop.

What the model is and is not trusted with:

  * **It drafts fields, descriptions, keywords, required rules and the ledger
    mapping.** All of it is data, checked by the same `Definition` validation
    as anything a person writes, and it lands as an unpublished draft.
  * **It never writes a transform.** A transform is code the server runs, and
    transforms are written by people (DESIGN §5.23). A drafted processor has
    none; if it needs arithmetic, the notes say so.
  * **Two lessons are enforced in code, not just asked for.** Top-level fields
    are always nullable -- a non-nullable amount is one the model will invent
    when the page has none -- so "must be present" becomes a `required` rule.
    And a draft that fails validation gets one repair attempt with the exact
    problems, then is handed to the author as YAML with the problems listed,
    rather than thrown away.

The model answers in a schema built for this, not `Definition`'s own: that
one keys fields by name, and a structured-output schema cannot have dynamic
keys. Fields come back as a list and are converted here. The schema has no
nullable values at all -- empty strings and empty lists stand for "none" --
so it never approaches the 16-union limit it would otherwise share with the
processors it describes.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from doctypes import authoring
from doctypes.authoring import AuthoringError
from doctypes.definition import FieldType
from llm.base import ExtractionError

#: Enough of a sample to learn its labels and layout from, without paying to
#: send a forty-page PDF.
SAMPLE_CHARS = 12_000
MAX_TOKENS = 8192

_NAME = r"^[a-z][a-z0-9_]{0,48}$"
_SCALARS = [t.value for t in FieldType if t is not FieldType.LIST]


def _field_schema(*, allow_list: bool) -> dict:
    properties: dict[str, Any] = {
        "name": {"type": "string", "pattern": _NAME},
        "type": {"type": "string", "enum": _SCALARS + (["list"] if allow_list else [])},
        "description": {"type": "string"},
        "can_be_missing": {"type": "boolean"},
        "choices": {"type": "array", "items": {"type": "string"}},
    }
    if allow_list:
        properties["item_fields"] = {"type": "array", "items": _field_schema(allow_list=False)}
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }


_COLUMN = {"type": "string", "description": "A field name, or empty for none."}

DRAFT_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "pattern": _NAME},
        "display_name": {"type": "string"},
        "description": {"type": "string"},
        "keywords": {"type": "array", "items": {"type": "string"}},
        "patterns": {"type": "array", "items": {"type": "string"}},
        "fields": {"type": "array", "items": _field_schema(allow_list=True)},
        "must_be_present": {"type": "array", "items": {"type": "string"}},
        "ledger": {
            "type": "object",
            "properties": {
                "direction": {"type": "string", "enum": ["out", "in", "none"]},
                "amount": _COLUMN,
                "occurred_on": _COLUMN,
                "counterparty": _COLUMN,
                "counterparty_tax_id": _COLUMN,
                "tax_amount": _COLUMN,
                "currency": _COLUMN,
            },
            "required": [
                "direction", "amount", "occurred_on", "counterparty",
                "counterparty_tax_id", "tax_amount", "currency",
            ],
            "additionalProperties": False,
        },
        "notes": {"type": "string"},
    },
    "required": [
        "name", "display_name", "description", "keywords", "patterns", "fields",
        "must_be_present", "ledger", "notes",
    ],
    "additionalProperties": False,
}

SYSTEM_PROMPT = """\
You design document processors for Ledgerline, a pipeline that reads business
documents (invoices, receipts, statements) and extracts structured data with a
language model. A processor says what to extract from one kind of document.
An author describes the document and what they need; you draft the processor.
They will review it, test it on real documents and publish it.

Fields
- One field per value the author needs. Names are lower_snake_case.
- Types: string, integer, number, money (any amount), date (YYYY-MM-DD),
  boolean, or list (a repeated group such as line items, with item_fields).
  `choices` is a closed set of allowed values for a string, else empty.
- The description is the instruction the extracting model reads, so it is
  the most important thing you write. Name the label the value sits next to
  ("labelled Invoice No or Invoice #"), say what it is NOT when there is a
  likely confusion ("not the due date"), and end with what to do when it is
  absent ("Null if not printed"). Prefer labels to positions: an issuer moves
  things around and keeps the labels.
- Never ask for a value to be calculated. The extracting model only reads; a
  total that is not printed must come back null. If the author needs
  arithmetic, say so in `notes` -- a person writes that as code afterwards.
- can_be_missing: true for almost everything. It is only false for a part of
  a list entry that always exists when the entry does (a line item's
  description). A value marked false is forced, and a forced value that is not
  on the page gets invented.
- At most 16 values may be able to be missing, counting list item parts.
  Mark list item parts that always exist as can_be_missing false to stay
  under it, and leave out what the author did not ask for.

Routing
- keywords: 5 to 9 words or short phrases that appear on this kind of
  document and distinguish it from others. Avoid words every financial
  document has ("total", "date", "amount"). Matching is case-insensitive
  and on whole words.
- patterns: regular expressions for structural signals, such as an invoice
  number format visible in the sample. Empty if there is nothing reliable.

Checks and ledger
- must_be_present: the fields without which the document is useless (often
  the total). They are checked after extraction, not forced on the model.
- ledger: how a document becomes a transaction. direction is "out" for money
  the business pays (a supplier invoice, a receipt), "in" for money it
  receives, "none" if it is not a transaction. amount must be a money field,
  occurred_on a date field, tax_amount a money field. Empty string for any
  column with no matching field.

notes: what the author should check or add -- assumptions you made, fields
you were unsure of, arithmetic that needs a transform. Plain sentences.\
"""


@dataclass
class Drafted:
    """The outcome of drafting: a draft row, or a definition still to fix."""

    #: Set when the draft validated and was saved.
    row: Any = None
    #: Set when it did not: the best attempt, as YAML, and why it failed.
    yaml: str | None = None
    problems: list[str] = field(default_factory=list)
    notes: str = ""
    model: str = ""
    cost_usd: Decimal = Decimal("0")
    attempts: int = 0


def draft(
    session,
    *,
    description: str,
    created_by: str,
    drafter,
    sample: str | None = None,
) -> Drafted:
    """
    Ask for a processor, check it, repair it once if needed, save it as a draft.

    `drafter` is anything with the extractor's `complete()` -- a real
    AnthropicExtractor built with the drafting model, or a fake in tests.
    """
    if not description.strip():
        raise AuthoringError("describe the document and what you need from it")

    existing = [s.name for s in authoring.list_processors(session)]
    user = _request(description, sample, existing)
    outcome = Drafted()
    problems: list[str] = []
    spec: dict | None = None
    answer: dict = {}

    for attempt in (1, 2):
        message = user if attempt == 1 else _repair(user, answer, problems)
        try:
            result = drafter.complete(
                system=SYSTEM_PROMPT, user=message, json_schema=DRAFT_SCHEMA,
                max_tokens=MAX_TOKENS, params={"purpose": "draft_processor", "attempt": attempt},
            )
        except ExtractionError as exc:
            if exc.spent is not None:
                outcome.cost_usd += exc.spent.cost_usd or Decimal("0")
            raise AuthoringError(f"the model call failed: {exc}") from exc
        outcome.attempts = attempt
        outcome.model = result.model
        outcome.cost_usd += result.cost_usd or Decimal("0")
        answer = result.data
        outcome.notes = answer.get("notes", "")

        try:
            spec = to_definition(answer)
            authoring.parse(spec)
        except AuthoringError as exc:
            problems = exc.details or [str(exc)]
            continue
        if spec["name"] in existing:
            problems = [f"name: {spec['name']!r} is already taken by another processor"]
            continue

        outcome.row = authoring.create_draft(
            session, spec, based_on=None, created_by=created_by
        )
        outcome.problems = []
        return outcome

    outcome.problems = problems
    outcome.yaml = authoring.dump_yaml(spec) if spec else None
    return outcome


def to_definition(answer: dict) -> dict:
    """The model's list-shaped answer as a `Definition` spec."""
    names = [f["name"] for f in answer.get("fields", [])]
    repeated = sorted({n for n in names if names.count(n) > 1})
    if repeated:
        raise AuthoringError(
            "fields must have different names", [f"fields: {n} appears twice" for n in repeated]
        )

    fields: dict[str, dict] = {}
    for f in answer.get("fields", []):
        spec: dict[str, Any] = {"type": f["type"], "description": f["description"]}
        if f.get("choices"):
            spec["choices"] = f["choices"]
        if f["type"] == "list":
            spec["item_fields"] = {
                item["name"]: _item(item) for item in f.get("item_fields", [])
            }
        # Never `required` at the top level, whatever the model said: see the
        # module docstring. Presence is the `required` rule's job.
        fields[f["name"]] = spec

    rules = [{"type": "required", "field": name} for name in answer.get("must_be_present", [])]

    definition: dict[str, Any] = {
        "name": answer["name"],
        "display_name": answer.get("display_name") or None,
        "description": answer.get("description") or None,
        "matches": {
            "keywords": answer.get("keywords", []),
            "patterns": answer.get("patterns", []),
            # The seeds' threshold: two signals out of the usual handful.
            "min_score": 0.2,
        },
        "fields": fields,
        "rules": rules,
    }
    ledger = answer.get("ledger") or {}
    if ledger.get("direction") in ("in", "out") and ledger.get("amount"):
        definition["canonical"] = {
            "direction": ledger["direction"],
            **{k: v for k, v in ledger.items() if k != "direction" and v},
        }
    return definition


def _item(item: dict) -> dict:
    spec: dict[str, Any] = {"type": item["type"], "description": item["description"]}
    if item.get("choices"):
        spec["choices"] = item["choices"]
    if not item.get("can_be_missing", True):
        spec["required"] = True
    return spec


def _request(description: str, sample: str | None, existing: list[str]) -> str:
    parts = [f"What the author needs:\n{description.strip()}"]
    if sample and sample.strip():
        clipped = sample.strip()[:SAMPLE_CHARS]
        parts.append(
            "A sample of this document, as the pipeline reads it (OCR or the PDF's "
            "text layer; layout approximate):\n<sample>\n" + clipped + "\n</sample>"
        )
    else:
        parts.append("No sample was given: base the labels on how such documents usually read.")
    if existing:
        parts.append(
            "Processors that already exist (choose a different name, and keywords "
            f"that tell this document apart from them): {', '.join(existing)}"
        )
    return "\n\n".join(parts)


def _repair(original: str, answer: dict, problems: list[str]) -> str:
    return (
        original
        + "\n\nYour previous draft was:\n"
        + json.dumps(answer, indent=2)
        + "\n\nIt was rejected for these reasons. Fix exactly these and keep the rest:\n- "
        + "\n- ".join(problems)
    )
