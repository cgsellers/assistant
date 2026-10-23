"""postgres: timezone-aware timestamps and jsonb

Revision ID: 47dd1c16de61
Revises: e31db512ae17
Create Date: 2026-10-23 20:08:23.176966

Postgres only; on SQLite this does nothing, because SQLite has one datetime
type and one JSON type and these columns are already what they should be.

Two things were wrong on Postgres, both invisible on SQLite:

  * Every timestamp after the first migration was `timestamp without time
    zone`, because `UTCDateTime` was built on a plain `DateTime` (see its
    docstring). Every stored value is UTC -- `utcnow()` is the only source --
    so the conversion reads them AT TIME ZONE 'UTC', which keeps each instant
    exactly where it was.
  * JSON columns were `json`, stored as text and re-parsed on every read.
    `jsonb` can be indexed and queried by key, which the ERP work will need.
    Except `document_types.definition`, which stays `json`: jsonb sorts
    object keys, and a processor's field order is meaningful.
"""
from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = '47dd1c16de61'
down_revision: str | Sequence[str] | None = 'e31db512ae17'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TIMESTAMPS = [
    ("api_keys", "created_at"),
    ("api_keys", "last_used_at"),
    ("api_keys", "revoked_at"),
    ("document_types", "created_at"),
    ("document_types", "updated_at"),
    ("extractions", "created_at"),
    ("transactions", "created_at"),
    ("user_sessions", "created_at"),
    ("user_sessions", "expires_at"),
    ("user_sessions", "revoked_at"),
    ("users", "created_at"),
    ("users", "disabled_at"),
    ("users", "last_login_at"),
    ("users", "locked_until"),
]

JSON_COLUMNS = [
    ("document_events", "detail"),
    ("extractions", "derived_fields"),
    ("extractions", "raw_json"),
    ("extractions", "transformed_json"),
    ("extractions", "validation_errors"),
    ("ocr_results", "layout_blocks"),
    ("ocr_results", "params"),
    ("transactions", "derived_fields"),
]


def _postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def upgrade() -> None:
    if not _postgres():
        return
    for table, column in TIMESTAMPS:
        op.execute(
            f'ALTER TABLE {table} ALTER COLUMN {column} TYPE timestamptz '
            f"USING {column} AT TIME ZONE 'UTC'"
        )
    for table, column in JSON_COLUMNS:
        op.execute(f"ALTER TABLE {table} ALTER COLUMN {column} TYPE jsonb USING {column}::jsonb")


def downgrade() -> None:
    if not _postgres():
        return
    for table, column in JSON_COLUMNS:
        op.execute(f"ALTER TABLE {table} ALTER COLUMN {column} TYPE json USING {column}::json")
    for table, column in TIMESTAMPS:
        op.execute(
            f"ALTER TABLE {table} ALTER COLUMN {column} TYPE timestamp "
            f"USING {column} AT TIME ZONE 'UTC'"
        )
