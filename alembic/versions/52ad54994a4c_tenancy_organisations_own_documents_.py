"""tenancy: organisations own documents, keys and users

Revision ID: 52ad54994a4c
Revises: 383f629709d5
Create Date: 2026-10-25 14:36:50.921791

DESIGN §9.3. Every document now belongs to an organisation, and so does
everything made from it; users and API keys belong to one too.

Existing rows all go to "Ledgerline demo", the team's own organisation,
created here: everything that exists today was uploaded by the team. Existing
users become staff for the same reason -- they are the team -- so the admin
panel keeps seeing every document. Existing keys do not: a key is never
staff, and from step 4.2 sees only its own organisation's documents.

Duplicate detection moves from "these bytes, anywhere" to "these bytes, in
this organisation": the global rule handed one client another's document.
"""
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = '52ad54994a4c'
down_revision: str | Sequence[str] | None = '383f629709d5'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

DEMO_SLUG = "ledgerline-demo"
DEMO_NAME = "Ledgerline demo"

#: Every table that gains an owner. Order matters only for readability.
OWNED = [
    "documents", "ocr_results", "document_events", "jobs",
    "extractions", "transactions", "api_keys", "users",
]


def upgrade() -> None:
    op.create_table(
        "organisations",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("slug", sa.String(length=60), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_organisations")),
        sa.UniqueConstraint("name", name=op.f("uq_organisations_name")),
    )
    with op.batch_alter_table("organisations", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_organisations_slug"), ["slug"], unique=True)

    demo_id = str(uuid.uuid4())
    organisations = sa.table(
        "organisations",
        sa.column("id", sa.String), sa.column("name", sa.String),
        sa.column("slug", sa.String), sa.column("created_at", sa.DateTime(timezone=True)),
    )
    op.bulk_insert(organisations, [{
        "id": demo_id, "name": DEMO_NAME, "slug": DEMO_SLUG, "created_at": datetime.now(UTC),
    }])

    # Nullable first, filled, then NOT NULL: adding a NOT NULL column to a
    # table with rows fails, and there is no default that would be honest.
    for table in OWNED:
        with op.batch_alter_table(table, schema=None) as batch_op:
            batch_op.add_column(sa.Column("organisation_id", sa.String(length=36), nullable=True))
        op.execute(sa.text(f"UPDATE {table} SET organisation_id = :demo").bindparams(demo=demo_id))
        with op.batch_alter_table(table, schema=None) as batch_op:
            batch_op.alter_column("organisation_id", existing_type=sa.String(length=36), nullable=False)
            batch_op.create_index(batch_op.f(f"ix_{table}_organisation_id"), ["organisation_id"], unique=False)
            batch_op.create_foreign_key(
                batch_op.f(f"fk_{table}_organisation_id_organisations"),
                "organisations", ["organisation_id"], ["id"],
            )

    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("is_staff", sa.Boolean(), server_default=sa.text("false"), nullable=False)
        )
    # Everyone with an account today is the team.
    op.execute(sa.text("UPDATE users SET is_staff = true"))

    with op.batch_alter_table("documents", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_documents_sha256"))
        batch_op.create_index(batch_op.f("ix_documents_sha256"), ["sha256"], unique=False)
        batch_op.create_unique_constraint(
            batch_op.f("uq_documents_organisation_id_sha256"), ["organisation_id", "sha256"]
        )


def downgrade() -> None:
    # Duplicates across organisations would break the global unique index
    # again; refusing beats silently deleting one client's document.
    bind = op.get_bind()
    clashes = bind.execute(sa.text(
        "SELECT sha256 FROM documents GROUP BY sha256 HAVING COUNT(*) > 1"
    )).fetchall()
    if clashes:
        raise RuntimeError(
            f"{len(clashes)} file(s) were uploaded by more than one organisation; "
            "a single global sha256 index cannot hold them. Resolve those first."
        )

    with op.batch_alter_table("documents", schema=None) as batch_op:
        batch_op.drop_constraint(batch_op.f("uq_documents_organisation_id_sha256"), type_="unique")
        batch_op.drop_index(batch_op.f("ix_documents_sha256"))
        batch_op.create_index(batch_op.f("ix_documents_sha256"), ["sha256"], unique=True)

    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.drop_column("is_staff")

    for table in reversed(OWNED):
        with op.batch_alter_table(table, schema=None) as batch_op:
            batch_op.drop_constraint(
                batch_op.f(f"fk_{table}_organisation_id_organisations"), type_="foreignkey"
            )
            batch_op.drop_index(batch_op.f(f"ix_{table}_organisation_id"))
            batch_op.drop_column("organisation_id")

    with op.batch_alter_table("organisations", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_organisations_slug"))
    op.drop_table("organisations")
