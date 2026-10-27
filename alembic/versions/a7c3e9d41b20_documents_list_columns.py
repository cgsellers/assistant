"""documents list columns

Revision ID: a7c3e9d41b20
Revises: f33b302db06f
Client portal backend, the documents list (DESIGN §9.4). What the list shows
for each document -- processor, supplier, its own date, its total -- as
columns on the document, set when it is read: a read model, because the
ledger only has rows for documents that passed their rules, and the list must
filter, search and sort on documents in review too. Plus the index the list
pages along.

Documents read before this revision have the columns empty until
`python -m doctypes.listing backfill` fills them from their current
extraction.

Create Date: 2026-10-27 15:20:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'a7c3e9d41b20'
down_revision: str | Sequence[str] | None = 'f33b302db06f'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('documents', sa.Column('doc_type', sa.String(length=64), nullable=True))
    op.add_column('documents', sa.Column('supplier', sa.String(length=200), nullable=True))
    op.add_column('documents', sa.Column('document_date', sa.Date(), nullable=True))
    op.add_column(
        'documents', sa.Column('total_amount', sa.Numeric(precision=14, scale=4), nullable=True)
    )
    op.add_column('documents', sa.Column('total_currency', sa.String(length=3), nullable=True))
    op.create_index(
        'ix_documents_listing', 'documents', ['organisation_id', 'uploaded_at', 'id'], unique=False
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_documents_listing', table_name='documents')
    with op.batch_alter_table('documents') as batch:
        batch.drop_column('total_currency')
        batch.drop_column('total_amount')
        batch.drop_column('document_date')
        batch.drop_column('supplier')
        batch.drop_column('doc_type')
