"""result tags

Stores each scenario's tags on its result rows, for the per-capability breakdown in
/results. Existing rows get an empty list.

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-25 18:07:08.930223
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("experiment_results", schema=None) as batch_op:
        # Existing rows predate tagging; give them an empty list rather than failing the
        # NOT NULL constraint on a populated table.
        batch_op.add_column(
            sa.Column("tags", sa.JSON(), nullable=False, server_default=sa.text("'[]'"))
        )


def downgrade() -> None:
    with op.batch_alter_table("experiment_results", schema=None) as batch_op:
        batch_op.drop_column("tags")
