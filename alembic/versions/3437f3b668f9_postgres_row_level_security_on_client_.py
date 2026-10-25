"""postgres row-level security on client data

Revision ID: 3437f3b668f9
Revises: 86a633e2a5f3
Create Date: 2026-10-25 21:32:58.688430

Tenancy step 4.3 (DESIGN §9.3): the database refuses another organisation's
rows itself, whatever the application asks -- raw SQL included. The second
lock behind core/scoping.py.

Each policy reads two settings the application sets at the start of every
transaction (core/scoping.py, `after_begin`): `app.organisation_id`, and
`app.all_organisations` for staff and the system's own processes. They are
transaction-local, so a pooled connection cannot carry one request's
organisation into the next; and a transaction that set neither sees no rows
at all -- the database fails closed too.

FORCE, because a table's owner otherwise bypasses its own policies. A
superuser always does, which is why the application connects as a role that
is neither (core/dbroles.py). Postgres only: SQLite has no row-level security,
and runs only on laptops and in the fast test run.
"""
from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = '3437f3b668f9'
down_revision: str | Sequence[str] | None = '86a633e2a5f3'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: The tables holding client data -- core.scoping.CLIENT_DATA, frozen here:
#: a migration must not change meaning because application code did.
CLIENT_TABLES = [
    "documents", "ocr_results", "document_events", "jobs", "extractions", "transactions",
]

#: `current_setting(name, true)` is NULL when the setting was never made, and
#: an empty string after a transaction that made it has ended; neither equals
#: an organisation id, and neither is 'on'.
RULE = (
    "current_setting('app.all_organisations', true) = 'on' "
    "OR organisation_id = current_setting('app.organisation_id', true)"
)


def _postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def upgrade() -> None:
    if not _postgres():
        return
    for table in CLIENT_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        # USING governs what is read, updated and deleted; WITH CHECK what may
        # be written -- so a session cannot insert a row for another client.
        op.execute(
            f"CREATE POLICY tenant_isolation ON {table} USING ({RULE}) WITH CHECK ({RULE})"
        )


def downgrade() -> None:
    if not _postgres():
        return
    for table in CLIENT_TABLES:
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")
