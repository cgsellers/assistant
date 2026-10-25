"""
Whether the database's schema is the one this code was written for.

`/health` answers "is the process up and can it reach the database", and an
empty database passes that: the deploy-new-code-and-forget-to-migrate failure
reported healthy while every real request failed (DESIGN §8.2). This is the
other question -- is the database at the migration this code expects -- asked
by `/ready` and by the worker before it takes a job.

The expected revision is read from the migration scripts shipped beside the
code, not configured, so the two cannot disagree.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import Engine

ALEMBIC_INI = Path(__file__).resolve().parents[1] / "alembic.ini"


@lru_cache(maxsize=1)
def expected_revision() -> str:
    """The head of the migrations that came with this code."""
    return ScriptDirectory.from_config(Config(str(ALEMBIC_INI))).get_current_head()


def current_revision(engine: Engine) -> str | None:
    """Where the database is; None if it has never been migrated."""
    with engine.connect() as connection:
        return MigrationContext.configure(connection).get_current_revision()


def problem(engine: Engine) -> str | None:
    """None when the schema matches the code; otherwise what is wrong, in a sentence."""
    current = current_revision(engine)
    expected = expected_revision()
    if current == expected:
        return None
    if current is None:
        return f"the database has not been migrated; run `alembic upgrade head` (to {expected})"
    return (
        f"the database is at migration {current} but this code expects {expected}; "
        "run `alembic upgrade head`"
    )
