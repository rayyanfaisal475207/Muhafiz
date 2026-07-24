"""add projects and project_id columns

Revision ID: fe8c28ae62b3
Revises: bb34463d5dd3
Create Date: 2026-07-15 21:49:46.440793+00:00

Phase 4 Step 0 schema-drift fix. Root cause: init_postgres()'s
create_all() fallback died on document_chunks's missing pgvector
extension, and since create_all() runs as one transaction, that failure
silently prevented projects, project_memory, sessions.project_id, and
documents.project_id from ever being created — even though all four have
existed in src/database/models.py since the Projects feature was built.
document_chunks itself was removed from models.py in this same Phase 4
Step 0 (superseded by ChromaDB, Phase 1; its removal was already
scheduled for Phase 6, pulled forward here as the actual fix).

Autogenerate also detected error_logs, ingestion_jobs, and
session_attachments (migration 003's raw-SQL tables, applied separately
via scripts/apply_migration.py) as drift. Those are unrelated to this
fix and have been hand-trimmed out, same discipline as the Phase 3
migration.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'fe8c28ae62b3'
down_revision: Union[str, None] = 'bb34463d5dd3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('projects',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('user_id', sa.UUID(), nullable=False),
    sa.Column('name', sa.Text(), nullable=False),
    sa.Column('description', sa.Text(), nullable=True),
    sa.Column('domain_context', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('project_memory',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('project_id', sa.UUID(), nullable=False),
    sa.Column('summary_text', sa.Text(), nullable=False),
    sa.Column('last_updated', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.add_column('documents', sa.Column('project_id', sa.UUID(), nullable=True))
    op.create_foreign_key(None, 'documents', 'projects', ['project_id'], ['id'], ondelete='CASCADE')
    op.add_column('sessions', sa.Column('project_id', sa.UUID(), nullable=True))
    op.alter_column('sessions', 'title',
               existing_type=sa.TEXT(),
               nullable=False)
    op.create_foreign_key(None, 'sessions', 'projects', ['project_id'], ['id'], ondelete='CASCADE')


def downgrade() -> None:
    op.drop_constraint(None, 'sessions', type_='foreignkey')
    op.alter_column('sessions', 'title',
               existing_type=sa.TEXT(),
               nullable=True)
    op.drop_column('sessions', 'project_id')
    op.drop_constraint(None, 'documents', type_='foreignkey')
    op.drop_column('documents', 'project_id')
    op.drop_table('project_memory')
    op.drop_table('projects')
