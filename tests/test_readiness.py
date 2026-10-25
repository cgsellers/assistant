"""
Readiness: is the database at the migration this code expects?

The gap it closes (DESIGN §8.2): `/health` returned 200 against an empty
schema, so a deploy that forgot to migrate looked healthy while every real
request failed.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

from core import migrations

from .conftest import ON_POSTGRES, sqlite_only

# On Postgres the test database is built by the real migrations and used
# through the application role, which may read alembic_version but never
# write it -- so the states below are faked on SQLite only, and Postgres
# checks the real one.


@pytest.fixture
def stamp(db_engine):
    """Mark the test database as migrated to `revision`, as alembic would."""

    def _stamp(revision: str) -> None:
        with db_engine.begin() as connection:
            connection.execute(text(
                "CREATE TABLE IF NOT EXISTS alembic_version "
                "(version_num VARCHAR(32) NOT NULL PRIMARY KEY)"
            ))
            connection.execute(text("DELETE FROM alembic_version"))
            connection.execute(
                text("INSERT INTO alembic_version VALUES (:v)"), {"v": revision}
            )

    yield _stamp
    # Not part of the models, so the per-test truncate on Postgres would leave it.
    with db_engine.begin() as connection:
        connection.execute(text("DROP TABLE IF EXISTS alembic_version"))


def test_health_does_not_care_about_migrations(anon_client):
    """Liveness stays liveness: an orchestrator restarts on it, not routes."""
    assert anon_client.get("/health").status_code == 200


@sqlite_only
def test_an_unmigrated_database_is_not_ready(anon_client):
    response = anon_client.get("/ready")
    assert response.status_code == 503
    assert "has not been migrated" in response.json()["reason"]


@sqlite_only
def test_a_database_behind_the_code_is_not_ready_and_says_where_it_is(anon_client, stamp):
    stamp("0123456789ab")
    response = anon_client.get("/ready")
    assert response.status_code == 503
    assert "at migration 0123456789ab" in response.json()["reason"]
    assert migrations.expected_revision() in response.json()["reason"]


def test_a_database_at_head_is_ready(anon_client, request):
    if not ON_POSTGRES:
        request.getfixturevalue("stamp")(migrations.expected_revision())
    response = anon_client.get("/ready")
    assert response.status_code == 200
    assert response.json() == {"status": "ready", "revision": migrations.expected_revision()}


def test_the_expected_revision_is_the_newest_migration_shipped():
    from pathlib import Path

    versions = Path(migrations.ALEMBIC_INI).parent / "alembic" / "versions"
    files = [p.read_text() for p in versions.glob("*.py")]
    head = migrations.expected_revision()
    assert any(f"revision: str = '{head}'" in f for f in files)
    assert not any(f"down_revision: str | Sequence[str] | None = '{head}'" in f for f in files)


def test_the_worker_takes_no_job_until_migrated(monkeypatch):
    from worker import run as worker

    answers = iter(["not migrated", "still behind", None])
    monkeypatch.setattr("core.migrations.problem", lambda engine: next(answers))

    worker.wait_for_migrations(poll_seconds=0)

    assert next(answers, "exhausted") == "exhausted"  # asked until it was ready
