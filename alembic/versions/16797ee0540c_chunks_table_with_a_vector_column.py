"""chunks table with a vector column

Revision ID: 16797ee0540c
Revises: 0a123456546c
Create Date: 2026-11-26 11:00:00.000000

Day 10: switches on the pgvector extension and adds the `chunks` table (a piece of a document
with its embedding). The index on the vector column comes in the next migration, so it can be
built and measured on its own.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = '16797ee0540c'
down_revision: Union[str, Sequence[str], None] = '0a123456546c'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # The extension adds the `vector` column type and the distance operators. The compose image
    # (pgvector/pgvector) already contains it; this switches it on in this database.
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.create_table(
        'chunks',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('tenant_id', sa.Uuid(), nullable=False),
        sa.Column('document_id', sa.Uuid(), nullable=False),
        sa.Column('chunk_index', sa.Integer(), nullable=False),
        sa.Column('heading', sa.String(length=512), nullable=False),
        sa.Column('text', sa.Text(), nullable=False),
        sa.Column('text_hash', sa.String(length=64), nullable=False),
        sa.Column('embedding', Vector(1024), nullable=False),
        sa.Column('embedding_model', sa.String(length=100), nullable=False),
        sa.Column('metadata', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['document_id'], ['documents.id'], name=op.f('fk_chunks_document_id_documents'), ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], name=op.f('fk_chunks_tenant_id_tenants'), ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_chunks')),
        sa.UniqueConstraint('document_id', 'chunk_index', name='uq_chunks_document_chunk_index'),
    )
    op.create_index(op.f('ix_chunks_document_id'), 'chunks', ['document_id'], unique=False)
    op.create_index(op.f('ix_chunks_tenant_id'), 'chunks', ['tenant_id'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_chunks_tenant_id'), table_name='chunks')
    op.drop_index(op.f('ix_chunks_document_id'), table_name='chunks')
    op.drop_table('chunks')
    # The extension is left installed: another table may use it, and dropping it needs superuser care.
