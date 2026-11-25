"""hnsw index on chunks embedding

Revision ID: dc013ced71fa
Revises: 16797ee0540c
Create Date: 2026-11-26 12:00:00.000000

Day 10: the approximate-nearest-neighbour index. It is its own migration so that building it can
be timed and measured separately from the table.

    vector_cosine_ops   built for cosine distance: a query must use the <=> operator
    m = 16              links kept per point (the pgvector default)
    ef_construction = 64   how carefully the graph is built (the pgvector default)

The search-time setting `hnsw.ef_search` is not part of the index; it is set per session.
"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'dc013ced71fa'
down_revision: Union[str, Sequence[str], None] = '16797ee0540c'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_index(
        'ix_chunks_embedding_hnsw',
        'chunks',
        ['embedding'],
        unique=False,
        postgresql_using='hnsw',
        postgresql_with={'m': 16, 'ef_construction': 64},
        postgresql_ops={'embedding': 'vector_cosine_ops'},
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_chunks_embedding_hnsw', table_name='chunks', postgresql_using='hnsw')
