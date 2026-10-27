"""ocr page sizes

Revision ID: c41e8a9b7d52
Revises: a7c3e9d41b20
Client portal backend, the document page (DESIGN §9.4). Each OCR result
records the size of every page it read, in the units of its boxes, so a box
can be placed on the page a browser shows: what lets the portal highlight
where a value was read. Results from before have none, and no locations.

Create Date: 2026-10-27 18:05:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'c41e8a9b7d52'
down_revision: str | Sequence[str] | None = 'a7c3e9d41b20'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        'ocr_results',
        sa.Column(
            'page_sizes', sa.JSON().with_variant(postgresql.JSONB(), 'postgresql'), nullable=True
        ),
    )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('ocr_results') as batch:
        batch.drop_column('page_sizes')
