"""
Drafting labels from model output, for a human to correct.

Labelling a corpus by hand is the slowest part of phase 3 and the only part
that cannot be automated away -- but most of it is transcription, and
transcription is what the extractor already does. So: run the pipeline over
unlabelled documents, write what it said into label files, and let a person
correct rather than type.

Every draft is written `verified: false`, and the runner refuses to score
unverified labels. That gap has to be recorded by the filesystem rather than
remembered, because a draft that silently becomes ground truth produces a
number that looks like evidence and is not -- the model would be marking its
own homework and scoring 100%.

Correcting means reading the document. A draft reads plausibly by
construction: `currency: EUR` on a Spanish receipt looks right until you
check the page and find no symbol printed anywhere.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from doctypes.definition import Definition, FieldSpec, FieldType
from evals.labels import DOCUMENT_SUFFIXES

log = logging.getLogger("evals.draft")


class _Quoted(str):
    """A string that must survive a round trip through a human editor."""


def _represent_quoted(dumper, data):
    return dumper.represent_scalar("tag:yaml.org,2002:str", str(data), style="'")


yaml.SafeDumper.add_representer(_Quoted, _represent_quoted)


@dataclass(frozen=True)
class Draft:
    name: str
    path: Path
    doc_type: str
    cost_usd: Any = None


def unlabelled(directory: Path) -> list[Path]:
    """
    Documents in a corpus with no label beside them.

    Existing labels are never touched. Re-running after correcting half a
    corpus has to be safe, or nobody will run it twice.
    """
    return sorted(
        path
        for path in directory.iterdir()
        if path.suffix.lower() in DOCUMENT_SUFFIXES
        and not path.with_suffix(".yaml").exists()
    )


def build(
    definition: Definition,
    *,
    extracted: dict,
    ocr_text: str,
    source: str,
    model: str,
) -> dict:
    """
    The label document, as a mapping ready to serialise.

    Fields come out in the order the document type declares them rather than
    alphabetically, so the file reads like the receipt it describes and a
    person checking it can work down the page.
    """
    expect: dict[str, Any] = {}
    for name, spec in definition.fields.items():
        expect[name] = _value(spec, extracted.get(name))

    stamp = datetime.now(UTC).strftime("%Y-%m-%d")
    return {
        "doc_type": definition.name,
        "verified": False,
        "notes": f"DRAFT from {model} on {stamp} -- check every field against the document",
        "source": source,
        "ocr_text": ocr_text,
        "expect": expect,
    }


def _value(spec: FieldSpec, value: Any) -> Any:
    """
    One field, shaped so a human editing the file cannot break it by accident.

    Money is force-quoted. `8,30` is a perfectly good YAML string and needs no
    quotes, but an unquoted amount invites someone to tidy it into `8.30`,
    which YAML then reads as the float 8.3 with the trailing zero gone. The
    quotes are there to teach the convention, not because the parser needs
    them.
    """
    if value is None:
        return None

    if spec.type is FieldType.LIST:
        if not isinstance(value, list):
            return None
        assert spec.item_fields is not None
        entries = []
        for entry in value:
            if not isinstance(entry, dict):
                continue
            entries.append(
                {
                    key: _value(item_spec, entry.get(key))
                    for key, item_spec in spec.item_fields.items()
                }
            )
        return entries

    if spec.type is FieldType.MONEY:
        return _Quoted(value)
    return value


HEADER = """\
# DRAFT LABEL -- not yet ground truth.
#
# A model produced the values below. Check each one against the document,
# correct what is wrong, then set `verified: true`. The eval runner refuses
# to score this file until you do.
#
# Checking means looking at the document. A draft reads plausibly by
# construction -- that is what makes it a useful starting point and what
# makes skimming it dangerous. The field most worth doubting is any value
# that looks obviously right for the kind of document this is: if the page
# does not print it, the correct answer is `null`.
#
# Amounts are quoted on purpose. Unquoted, YAML reads 8.30 as the float 8.3
# and the precision is gone before anything sees it.
"""


def write(path: Path, document: dict) -> Path:
    """Serialise a draft, refusing to clobber an existing label."""
    if path.exists():
        raise FileExistsError(f"{path.name} already exists; drafts never overwrite")
    body = yaml.safe_dump(document, sort_keys=False, allow_unicode=True, width=100)
    path.write_text(HEADER + "\n" + body)
    return path
