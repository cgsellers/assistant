"""corrections

Revision ID: e5b91c3f0a27
Revises: c41e8a9b7d52
Client portal backend, corrections (DESIGN §9.4). People correct what was
read from a document, or approve it as it is: a `corrections` row per save
-- who, what, from what -- and a revision on each document, which every
save must name, so two people saving at once cannot both win.

`corrections` holds a client's data, so on Postgres it gets the same
row-level security as the other client tables (3437f3b668f9).

Create Date: 2026-10-27 21:10:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'e5b91c3f0a27'
down_revision: str | Sequence[str] | None = 'c41e8a9b7d52'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Frozen here, as in 3437f3b668f9: a migration must not change meaning
#: because application code did.
RULE = (
    "current_setting('app.all_organisations', true) = 'on' "
    "OR organisation_id = current_setting('app.organisation_id', true)"
)


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        'documents', sa.Column('revision', sa.Integer(), server_default='1', nullable=False)
    )
    op.create_table(
        'corrections',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('document_id', sa.String(length=36), nullable=False),
        sa.Column('organisation_id', sa.String(length=36), nullable=False),
        sa.Column('extraction_id', sa.String(length=36), nullable=False),
        sa.Column('revision', sa.Integer(), nullable=False),
        sa.Column('by', sa.String(length=200), nullable=False),
        sa.Column(
            'changes', sa.JSON().with_variant(postgresql.JSONB(), 'postgresql'), nullable=False
        ),
        sa.Column('approved', sa.Boolean(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ['document_id'], ['documents.id'], name=op.f('fk_corrections_document_id_documents')
        ),
        sa.ForeignKeyConstraint(
            ['extraction_id'], ['extractions.id'],
            name=op.f('fk_corrections_extraction_id_extractions'),
        ),
        sa.ForeignKeyConstraint(
            ['organisation_id'], ['organisations.id'],
            name=op.f('fk_corrections_organisation_id_organisations'),
        ),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_corrections')),
    )
    op.create_index(op.f('ix_corrections_document_id'), 'corrections', ['document_id'])
    op.create_index(op.f('ix_corrections_organisation_id'), 'corrections', ['organisation_id'])
    if op.get_bind().dialect.name == 'postgresql':
        op.execute("ALTER TABLE corrections ENABLE ROW LEVEL SECURITY")
        op.execute("ALTER TABLE corrections FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY tenant_isolation ON corrections USING ({RULE}) WITH CHECK ({RULE})"
        )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_corrections_organisation_id'), table_name='corrections')
    op.drop_index(op.f('ix_corrections_document_id'), table_name='corrections')
    op.drop_table('corrections')
    with op.batch_alter_table('documents') as batch:
        batch.drop_column('revision')
