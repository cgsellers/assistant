"""
Labelled documents: the ground truth an eval run scores against.

A label is a YAML file sitting beside the document it describes and sharing
its stem -- `luna-001.png` and `luna-001.yaml`. One file per document, so
adding a receipt never edits anything that already exists.

YAML rather than JSON because a label records a *judgement*, and the
judgement is often the valuable part:

    currency: null        # no symbol or code printed anywhere
    tax_amount: '0,75'    # two IVA lines, 10% and 21%, summed

Six months later, when a field starts failing, the first question is whether
the code is wrong or the label is. A comment answers it; JSON cannot hold
one. Valid JSON is still valid YAML, so a labelling UI that writes `.json`
later will load without changes.

The cost of YAML is that it silently turns an unquoted `8.30` into a float,
and this is a system whose first rule is that money is Decimal and never
float. `check()` catches that at load time rather than letting it become a
mysteriously-failing eval.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, model_validator

from doctypes.definition import Definition, FieldSpec, FieldType

#: Extensions a label may sit beside. The label itself is always `.yaml`.
DOCUMENT_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp", ".tiff", ".pdf")


class Label(BaseModel):
    """What one document is supposed to extract to."""

    model_config = ConfigDict(extra="forbid")

    #: Which document type this should route to. Checked, not assumed: a
    #: receipt the router sends to `invoice` is a routing failure worth
    #: measuring separately from a field being wrong.
    doc_type: str

    #: field name -> the correct value. A field the document genuinely does
    #: not carry is `null`, which is a real expectation and not a gap: a model
    #: that invents a value for it has failed, and that is the failure this
    #: corpus exists to catch.
    expect: dict[str, Any]

    #: The document to read, relative to the label. Omit when `ocr_text` is
    #: given and the original is not in the corpus.
    source: str | None = None

    #: Cached OCR text. Supplying it skips the container, which makes a run
    #: reproducible and fast; omitting it measures OCR and extraction
    #: together. Phase 3 wants both, at different times.
    ocr_text: str | None = None

    #: Anything a human needed to decide. Free text, read by people.
    notes: str | None = None

    @model_validator(mode="after")
    def _needs_something_to_read(self) -> Label:
        if not (self.source or self.ocr_text):
            raise ValueError("a label needs either `source` or `ocr_text`")
        return self


@dataclass(frozen=True)
class LabelledDocument:
    """A label plus where it came from, so errors can name the file."""

    name: str
    path: Path
    label: Label

    def document_path(self) -> Path | None:
        """
        The file this label describes, if it is on disk.

        An explicit `source` wins. Otherwise the stem is tried against the
        known suffixes, so a label beside its receipt needs no `source` line.
        """
        if self.label.source:
            candidate = self.path.parent / self.label.source
            return candidate if candidate.exists() else None
        for suffix in DOCUMENT_SUFFIXES:
            candidate = self.path.with_suffix(suffix)
            if candidate.exists():
                return candidate
        return None


def load_corpus(directory: Path) -> list[LabelledDocument]:
    """
    Every label in a directory, sorted by name so runs are comparable.

    A malformed label raises with its filename. Loading is deliberately
    separate from `check`: this catches YAML and shape errors without needing
    a document type, so a corpus can be validated before the database exists.
    """
    documents = []
    for path in sorted(directory.glob("*.yaml")):
        raw = yaml.safe_load(path.read_text())
        if raw is None:
            raise ValueError(f"{path.name} is empty")
        try:
            label = Label.model_validate(raw)
        except Exception as exc:
            raise ValueError(f"{path.name} is not a valid label: {exc}") from exc
        documents.append(LabelledDocument(name=path.stem, path=path, label=label))
    return documents


def check(document: LabelledDocument, definition: Definition) -> list[str]:
    """
    Problems with a label, as readable messages. Empty means it is usable.

    Separate from loading because it needs the document type, and because a
    run should refuse to start on a bad corpus rather than quietly score
    against expectations that cannot be met. A label naming a field the type
    does not have would otherwise count as a miss on every single document.
    """
    problems: list[str] = []
    label = document.label

    if label.doc_type != definition.name:
        problems.append(
            f"doc_type is {label.doc_type!r} but was checked against "
            f"{definition.name!r}"
        )

    unknown = set(label.expect) - set(definition.fields)
    if unknown:
        problems.append(f"expects unknown field(s): {sorted(unknown)}")

    for name, spec in definition.fields.items():
        if name not in label.expect:
            continue
        problems.extend(_check_value(spec, label.expect[name], name))

    return [f"{document.name}: {p}" for p in problems]


def _check_value(spec: FieldSpec, value: Any, path: str) -> list[str]:
    if value is None:
        return []

    if spec.type is FieldType.LIST:
        if not isinstance(value, list):
            return [f"{path} should be a list, got {type(value).__name__}"]
        problems = []
        assert spec.item_fields is not None
        for i, entry in enumerate(value):
            if not isinstance(entry, dict):
                problems.append(f"{path}[{i}] should be a mapping")
                continue
            unknown = set(entry) - set(spec.item_fields)
            if unknown:
                problems.append(f"{path}[{i}] has unknown key(s): {sorted(unknown)}")
            for key, item_spec in spec.item_fields.items():
                if key in entry:
                    problems.extend(_check_value(item_spec, entry[key], f"{path}[{i}].{key}"))
        return problems

    if spec.type is FieldType.MONEY and isinstance(value, float):
        # YAML's doing, not the author's: an unquoted 8.30 arrives as 8.3 and
        # the trailing zero -- and any further precision -- is already gone.
        # Refusing is the only honest response; there is nothing left to
        # recover from the parsed value.
        return [
            f"{path} is the float {value!r}; quote it (e.g. '{value:.2f}') so "
            f"no precision is lost"
        ]

    if spec.choices and value not in spec.choices:
        return [f"{path} is {value!r}, which is not one of {spec.choices}"]

    return []


def check_corpus(documents: list[LabelledDocument], definition: Definition) -> list[str]:
    """Every problem across a corpus, so one run reports them all."""
    return [p for document in documents for p in check(document, definition)]
