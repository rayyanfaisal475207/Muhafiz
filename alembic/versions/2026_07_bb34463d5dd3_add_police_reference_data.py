"""add police_reference_data

Revision ID: bb34463d5dd3
Revises: ede34718e0cf
Create Date: 2026-07-15 21:18:11.183101+00:00

Autogenerate also detected a large amount of pre-existing schema drift
unrelated to this change (document_chunks, projects, project_memory,
session_attachments, error_logs, ingestion_jobs — either created via
init_postgres()'s create_all() fallback, which fails locally on
document_chunks's missing pgvector extension, or via the separate
migrations/003_admin_dashboard_and_attachments.sql raw-SQL path). That
drift is out of scope for Phase 3 and has been hand-trimmed out of this
migration, which creates only police_reference_data.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'bb34463d5dd3'
down_revision: Union[str, None] = 'ede34718e0cf'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('police_reference_data',
    sa.Column('ref_id', sa.UUID(), nullable=False),
    sa.Column('category', sa.Text(), nullable=False),
    sa.Column('subject', sa.Text(), nullable=False),
    sa.Column('description', sa.Text(), nullable=True),
    sa.Column('fine_amount', sa.Numeric(precision=12, scale=2), nullable=True),
    sa.Column('section_ref', sa.Text(), nullable=True),
    sa.Column('source_document', sa.Text(), nullable=True),
    sa.Column('source_type', sa.Text(), nullable=False),
    sa.Column('effective_from', sa.Date(), nullable=True),
    sa.Column('created_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("source_type IN ('scraped', 'synthetic')", name='ck_police_reference_data_source_type'),
    sa.PrimaryKeyConstraint('ref_id')
    )


def downgrade() -> None:
    op.drop_table('police_reference_data')
