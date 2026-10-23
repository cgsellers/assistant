"""
Loading seed document types from disk into `document_types`.

A fresh clone has an empty table, so every document routes to `unsupported`.
These files are what make the pipeline do something before an analyst has
authored anything.

Seeds are YAML rather than a Python module or an Alembic data migration, and
both alternatives were rejected for the same reason: a document type is data.
A module makes it code again, and baking a definition into a migration freezes
one version into a file that must never change while the table it seeded goes
on being edited.

Run it with:  uv run python -m doctypes.seed
"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

import yaml
from pydantic import ValidationError
from sqlalchemy.orm import Session

from core import repository
from doctypes import transform
from doctypes.definition import Definition

log = logging.getLogger("doctypes.seed")

SEED_DIR = Path(__file__).parent / "seeds"

_PASSED: set[str] = set()


def load_seeds(directory: Path | None = None) -> list[Definition]:
    """
    Parse every seed file, or raise with the filename if one is malformed.

    Validation happens here rather than at insert time so a typo is a startup
    error naming the file, not a row that fails to parse on the first document
    that routes to it.
    """
    directory = directory or SEED_DIR
    definitions = []
    for path in sorted(directory.glob("*.yaml")):
        raw = yaml.safe_load(path.read_text())
        try:
            definitions.append(Definition.model_validate(_attach_transform(raw, path)))
        except Exception as exc:
            raise ValueError(f"{path.name} is not a valid document type: {exc}") from exc
    return definitions


def _attach_transform(raw, path: Path):
    """
    Read `transform: {file: x.py}` into `transform.code`.

    The code can live in a real .py file beside the seed -- where an editor
    and a linter can see it -- but the stored definition always carries the
    source itself, so each version records the code it ran.
    """
    spec = raw.get("transform") if isinstance(raw, dict) else None
    if not isinstance(spec, dict) or "file" not in spec:
        return raw
    if "code" in spec:
        raise ValueError("transform takes `code` or `file`, not both")
    source = (path.parent / spec["file"]).resolve()
    if source.parent != path.parent.resolve():
        raise ValueError(f"transform file {spec['file']!r} must sit beside the seed")
    code = source.read_text()
    attached = {k: v for k, v in spec.items() if k != "file"} | {"code": code}
    return raw | {"transform": attached}


def check_transforms(definitions: list[Definition]) -> None:
    """
    Run every transform against its examples, or raise naming the type.

    Before anything is written, so a broken transform never becomes the
    active version. Not part of `load_seeds`: that runs on every test and
    every import of the seeds, and this starts an interpreter per example.
    """
    for definition in definitions:
        # The same code, examples and field types give the same answer, so a
        # pass is remembered for the life of the process. Every test that
        # seeds a fresh database would otherwise start five interpreters.
        key = definition.model_dump_json(include={"fields", "transform"})
        if key in _PASSED:
            continue
        failures = transform.check_examples(definition)
        if not failures:
            _PASSED.add(key)
        if failures:
            raise ValueError(
                f"{definition.name}: the transform fails its examples:\n  "
                + "\n  ".join(failures)
            )


def sync(session: Session, definitions: list[Definition]) -> dict[str, str]:
    """
    Make the table match the seed files. Returns name -> what happened.

    Idempotent, because this runs on every deploy and on every developer's
    machine: a seed whose content already matches the active version writes
    nothing. Without that, re-running would pile up identical versions and
    make the version number meaningless as a record of real change.

    A changed seed writes a new version rather than editing the active row,
    for the reason the table is append-only -- extractions reference the
    version that produced them.
    """
    # Decide everything before writing anything, so the examples of every
    # type about to change can run first: a broken transform must never
    # become the active version, and nor should the types seeded beside it.
    actions: dict[str, str] = {}
    pending = []
    for definition in definitions:
        active = _changed(session, definition)
        if active is False:
            current = repository.get_active_document_type(session, definition.name)
            actions[definition.name] = f"unchanged (v{current.version})"
        else:
            pending.append((definition, active))
    check_transforms([d for d, _ in pending])

    for definition, active in pending:
        row = repository.create_document_type_version(session, definition)
        actions[definition.name] = (
            f"created (v{row.version})" if active is None else f"updated (v{row.version})"
        )

    return actions


def _changed(session: Session, definition: Definition):
    """
    False if the active version already matches; otherwise the active row,
    or None when there is none yet.
    """
    active = repository.get_active_document_type(session, definition.name)
    if active is None:
        return None
    # Compare at the stored version number: the seed file does not carry one
    # worth trusting, and a version bump alone is not a content change.
    # Both sides through the same model, so a field added to the definition
    # *format* -- with a default -- does not read as a change to every stored
    # type. Comparing raw JSON did exactly that: adding `canonical.tax_lines`
    # versioned the receipt type, which had not changed at all.
    try:
        stored = Definition.model_validate(active.definition)
    except ValidationError:
        # The stored version breaks a rule added since it was written -- e.g.
        # the cap on nullable fields. The registry already skips it, so the
        # type is not routing at all until a valid version lands; treating it
        # as changed is what gets one written.
        return active
    same = definition.model_copy(update={"version": active.version}) == stored.model_copy(
        update={"version": active.version}
    )
    return False if same else active


def main() -> None:
    parser = argparse.ArgumentParser(description="Load seed document types")
    parser.add_argument(
        "--dir", type=Path, default=None, help="override the seed directory"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="parse and report, write nothing"
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    definitions = load_seeds(args.dir)

    if args.dry_run:
        check_transforms(definitions)
        for definition in definitions:
            log.info(
                "%s v%s: %d fields, %d rules, %d profiles",
                definition.name,
                definition.version,
                len(definition.fields),
                len(definition.rules),
                len(definition.profiles),
            )
        return

    from core.db import SessionLocal

    with SessionLocal() as session:
        for name, action in sync(session, definitions).items():
            log.info("%-12s %s", name, action)


if __name__ == "__main__":
    main()
