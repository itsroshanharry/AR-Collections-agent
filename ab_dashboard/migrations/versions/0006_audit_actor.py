"""audit actor

Who did what: the actor on every invoice event (an API role, the scheduler, the voice
agent or "system"), who resolved each task and each bank exception, and who started each
experiment. Rows written before this migration are marked "system", which is what they
were: there was no authentication to record anyone else.

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-28 11:30:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("invoice_events", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("actor", sa.String(length=64), nullable=False, server_default="system")
        )
    with op.batch_alter_table("tasks", schema=None) as batch_op:
        batch_op.add_column(sa.Column("resolved_by", sa.String(length=64), nullable=True))
    with op.batch_alter_table("bank_transactions", schema=None) as batch_op:
        batch_op.add_column(sa.Column("resolved_by", sa.String(length=64), nullable=True))
    with op.batch_alter_table("experiments", schema=None) as batch_op:
        batch_op.add_column(sa.Column("created_by", sa.String(length=64), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("experiments", schema=None) as batch_op:
        batch_op.drop_column("created_by")
    with op.batch_alter_table("bank_transactions", schema=None) as batch_op:
        batch_op.drop_column("resolved_by")
    with op.batch_alter_table("tasks", schema=None) as batch_op:
        batch_op.drop_column("resolved_by")
    with op.batch_alter_table("invoice_events", schema=None) as batch_op:
        batch_op.drop_column("actor")
