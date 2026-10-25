"""
The second lock: Postgres row-level security (tenancy step 4.3).

These go around the application on purpose -- raw SQL, which core/scoping.py
does not see -- because what is under test is whether the *database* refuses.
Postgres only; on SQLite they are skipped, and the application layer is the
only lock there.

The first test matters most. A superuser, or a role with BYPASSRLS, ignores
every policy, so if CI ever connected as one these tests would pass for the
wrong reason. It checks the role is really bound.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, ProgrammingError

from core import repository, tenancy
from core.dbroles import APP_ROLE, describe
from core.scoping import Scope, set_scope

from .conftest import ON_POSTGRES

pytestmark = pytest.mark.skipif(not ON_POSTGRES, reason="row-level security is Postgres only")


@pytest.fixture
def world(session):
    ids = {}
    for slug in ("acme", "globex"):
        org = tenancy.create(session, slug.title(), slug)
        document, _ = repository.ingest(
            session, organisation_id=org.id, data=slug.encode() * 8,
            mime_type="image/png", original_filename=f"{slug}.png",
        )
        ids[slug] = {"org": org.id, "document": document.id}
    return ids


@pytest.fixture
def raw(session_factory):
    """A session as `scope` sees it, for raw SQL the application layer never inspects."""
    opened = []

    def _open(scope: Scope | None):
        s = session_factory()
        if scope is not None:
            set_scope(s, scope)
        opened.append(s)
        return s

    yield _open
    for s in opened:
        s.close()


def _count(s) -> int:
    return s.execute(text("SELECT count(*) FROM documents")).scalar()


def test_the_application_connects_as_a_role_the_policies_bind(db_engine):
    with db_engine.connect() as connection:
        assert connection.execute(text("SELECT current_user")).scalar() == APP_ROLE
        assert describe(connection) == {
            "superuser": False, "bypasses_rls": False, "owns_tables": 0,
        }


def test_raw_sql_sees_only_its_own_organisation(raw, world):
    assert _count(raw(Scope.of(world["acme"]["org"], "acme"))) == 1
    assert _count(raw(Scope.everyone("staff"))) == 2


def test_a_transaction_that_said_nothing_sees_nothing(raw, world):
    """The database fails closed too: no scope, no rows -- not every row."""
    assert _count(raw(None)) == 0


def test_another_organisations_rows_cannot_be_changed(raw, world):
    acme = raw(Scope.of(world["acme"]["org"], "acme"))
    changed = acme.execute(
        text("UPDATE documents SET original_filename = 'mine' WHERE id = :id"),
        {"id": world["globex"]["document"]},
    )
    assert changed.rowcount == 0
    deleted = acme.execute(
        text("DELETE FROM document_events WHERE document_id = :id"),
        {"id": world["globex"]["document"]},
    )
    assert deleted.rowcount == 0


def test_a_row_cannot_be_written_for_another_organisation(raw, world):
    """WITH CHECK: not just reading -- writing as someone else is refused."""
    acme = raw(Scope.of(world["acme"]["org"], "acme"))
    with pytest.raises(ProgrammingError, match="row-level security"):
        acme.execute(
            text(
                "INSERT INTO document_events (id, document_id, organisation_id, to_status, "
                "created_at) VALUES ('x', :doc, :org, 'forged', now())"
            ),
            {"doc": world["globex"]["document"], "org": world["globex"]["org"]},
        )


def test_the_scope_ends_with_the_transaction(db_engine, world):
    """
    `set_config(..., true)` is transaction-local, so a pooled connection
    cannot carry one request's organisation into the next one's transaction.
    One connection, reused: scoped, committed, then asked again with nothing.
    """
    from sqlalchemy.orm import Session

    with db_engine.connect() as connection:
        with Session(bind=connection) as first:
            set_scope(first, Scope.of(world["acme"]["org"], "acme"))
            assert _count(first) == 1
            first.commit()
        connection.commit()
        assert connection.execute(text("SELECT count(*) FROM documents")).scalar() == 0


def test_the_application_role_cannot_rewrite_history_or_the_schema(db_engine):
    with db_engine.connect() as connection:
        for statement in (
            "UPDATE alembic_version SET version_num = 'x'",
            "CREATE TABLE sneaky (id int)",
            "TRUNCATE documents",
            "ALTER TABLE documents DISABLE ROW LEVEL SECURITY",
        ):
            with pytest.raises(DBAPIError):
                connection.execute(text(statement))
            connection.rollback()
