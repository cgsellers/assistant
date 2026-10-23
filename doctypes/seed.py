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

Since the panel, seeds only *create*: they are the starter set for a fresh
database -- a laptop, CI, the tests -- and never touch a processor that
already exists. An existing one changes in the panel. So `doctypes/seeds/`
is not production's copy, and is expected to fall behind it.

Run it with:  uv run python -m doctypes.seed
"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

import yaml
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from core import repository
from core.models import DocumentTypeDefinition
from doctypes import transform
from doctypes.definition import Definition

log = logging.getLogger("doctypes.seed")

SEED_DIR = Path(__file__).parent / "seeds"


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
        failures = transform.check_examples(definition)
        if failures:
            raise ValueError(
                f"{definition.name}: the transform fails its examples:\n  "
                + "\n  ".join(failures)
            )


def sync(session: Session, definitions: list[Definition]) -> dict[str, str]:
    """
    Create the processors that do not exist yet. Returns name -> what happened.

    Create-only, never update. The panel is where an existing processor
    changes (DESIGN §9.1), so a seed that differs from what is stored is
    reported and left alone: overwriting it on deploy would quietly undo
    whatever was published in the panel. Nor is a retired processor brought
    back -- retiring it was a decision.

    Idempotent, because this runs on every deploy and on every developer's
    machine: when everything exists, it writes nothing.
    """
    actions: dict[str, str] = {}
    missing = []
    for definition in definitions:
        state = _state(session, definition)
        if state is None:
            missing.append(definition)
        else:
            actions[definition.name] = state

    # Every new type's examples run before any is written, so a broken
    # transform never becomes live -- and nor do the types seeded beside it.
    check_transforms(missing)
    for definition in missing:
        row = repository.create_document_type_version(session, definition, created_by="seed")
        actions[definition.name] = f"created (v{row.version})"
    return actions


def replace(session: Session, definitions: list[Definition], names: list[str]) -> dict[str, str]:
    """
    Publish the seed file as a new live version, over whatever is stored.

    The break-glass, not the workflow. It exists for one case the panel
    cannot handle: a stored version that no longer validates -- a rule added
    to the format since it was written, say -- so the registry skips it, the
    type stops routing, and the panel cannot open it to start a draft. It
    overwrites on purpose, so it is only ever run by name.
    """
    by_name = {d.name: d for d in definitions}
    unknown = sorted(set(names) - set(by_name))
    if unknown:
        raise ValueError(f"no seed file for {unknown}")
    chosen = [by_name[n] for n in names]
    check_transforms(chosen)

    from doctypes.authoring import latest_published

    actions = {}
    for definition in chosen:
        row = repository.create_document_type_version(
            session,
            definition,
            created_by="seed:replace",
            based_on_version=latest_published(session, definition.name),
        )
        actions[definition.name] = f"replaced (now v{row.version})"
    return actions


def _state(session: Session, definition: Definition) -> str | None:
    """None if the processor does not exist; otherwise why it is left alone."""
    from doctypes.authoring import latest_published

    if latest_published(session, definition.name) is None:
        drafts = session.scalar(
            select(func.count(DocumentTypeDefinition.id)).where(
                DocumentTypeDefinition.name == definition.name
            )
        )
        # Only drafts exist: somebody is creating this type in the panel.
        return f"drafted in the panel ({drafts}), left alone" if drafts else None

    active = repository.get_active_document_type(session, definition.name)
    if active is None:
        return "retired, left alone"

    # Both sides through the same model, so a field added to the definition
    # *format* -- with a default -- does not read as a difference in every
    # stored type. Comparing raw JSON did exactly that: adding
    # `canonical.tax_lines` made the receipt type look changed.
    try:
        stored = Definition.model_validate(active.definition)
    except ValidationError:
        return (
            f"v{active.version} no longer validates, so it is not routing; "
            f"fix with: python -m doctypes.seed --replace {definition.name}"
        )
    same = definition.model_copy(update={"version": active.version}) == stored.model_copy(
        update={"version": active.version}
    )
    if same:
        return f"unchanged (v{active.version})"
    return f"v{active.version} differs from the seed file, left alone (edit it in the panel)"


def main() -> None:
    parser = argparse.ArgumentParser(description="Load seed document types")
    parser.add_argument(
        "--dir", type=Path, default=None, help="override the seed directory"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="parse and report, write nothing"
    )
    parser.add_argument(
        "--replace", action="append", default=[], metavar="NAME",
        help="publish this seed over what is stored -- the break-glass, see replace()",
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
        actions = (
            replace(session, definitions, args.replace) if args.replace
            else sync(session, definitions)
        )
        for name, action in actions.items():
            log.info("%-12s %s", name, action)


if __name__ == "__main__":
    main()
