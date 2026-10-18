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
from sqlalchemy.orm import Session

from core import repository
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
            definitions.append(Definition.model_validate(raw))
        except Exception as exc:
            raise ValueError(f"{path.name} is not a valid document type: {exc}") from exc
    return definitions


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
    actions: dict[str, str] = {}

    for definition in definitions:
        active = repository.get_active_document_type(session, definition.name)

        if active is not None:
            # Compare at the stored version number: the seed file does not
            # carry one worth trusting, and a version bump alone is not a
            # content change.
            same = (
                definition.model_copy(update={"version": active.version}).model_dump(
                    mode="json"
                )
                == active.definition
            )
            if same:
                actions[definition.name] = f"unchanged (v{active.version})"
                continue

        row = repository.create_document_type_version(session, definition)
        actions[definition.name] = (
            f"created (v{row.version})" if active is None else f"updated (v{row.version})"
        )

    return actions


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
