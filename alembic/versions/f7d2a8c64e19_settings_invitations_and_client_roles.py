"""settings: invitations and client roles

Revision ID: f7d2a8c64e19
Revises: e5b91c3f0a27
Client portal backend, settings (DESIGN §9.4). Admins at a client invite
people by link (`invitations`, the token kept only as a hash) and make keys
for their own systems (`api_keys.created_by` says who did).

Clients get their own roles -- admin, member, viewer -- in place of the
team's processor roles, which mean nothing to them. A client's users and keys
holding "author" were members in all but name, and become "member" here; the
team's own accounts are left as they are.

Create Date: 2026-10-28 10:30:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'f7d2a8c64e19'
down_revision: str | Sequence[str] | None = 'e5b91c3f0a27'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CLIENTS = "SELECT id FROM organisations WHERE NOT is_internal"


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'invitations',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('organisation_id', sa.String(length=36), nullable=False),
        sa.Column('email', sa.String(length=254), nullable=False),
        sa.Column('role', sa.String(length=10), nullable=False),
        sa.Column('token_hash', sa.String(length=64), nullable=False),
        sa.Column('created_by', sa.String(length=254), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('accepted_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('revoked_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ['organisation_id'], ['organisations.id'],
            name=op.f('fk_invitations_organisation_id_organisations'),
        ),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_invitations')),
    )
    op.create_index(op.f('ix_invitations_email'), 'invitations', ['email'])
    op.create_index(op.f('ix_invitations_organisation_id'), 'invitations', ['organisation_id'])
    op.create_index(op.f('ix_invitations_token_hash'), 'invitations', ['token_hash'], unique=True)
    op.add_column('api_keys', sa.Column('created_by', sa.String(length=254), nullable=True))

    op.execute(f"UPDATE users SET role = 'member' WHERE role = 'author' AND organisation_id IN ({CLIENTS})")
    op.execute(
        f"UPDATE api_keys SET role = 'member' WHERE role = 'author' AND organisation_id IN ({CLIENTS})"
    )


def downgrade() -> None:
    """Downgrade schema."""
    # Back to the vocabulary the code before this knew.
    op.execute("UPDATE users SET role = 'author' WHERE role IN ('member', 'viewer')")
    op.execute("UPDATE api_keys SET role = 'author' WHERE role IN ('member', 'viewer')")
    with op.batch_alter_table('api_keys') as batch:
        batch.drop_column('created_by')
    op.drop_index(op.f('ix_invitations_token_hash'), table_name='invitations')
    op.drop_index(op.f('ix_invitations_organisation_id'), table_name='invitations')
    op.drop_index(op.f('ix_invitations_email'), table_name='invitations')
    op.drop_table('invitations')
