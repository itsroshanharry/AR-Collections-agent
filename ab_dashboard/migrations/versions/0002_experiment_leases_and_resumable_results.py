"""experiment leases and resumable results

Persists each run's seed and scenario limit so any worker can resume it, adds the
worker lease (worker_id + heartbeat_at), and makes (experiment, scenario, variant)
unique so per-scenario writes are idempotent.

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-23 18:11:08.703339
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("experiment_results", schema=None) as batch_op:
        batch_op.create_unique_constraint(
            "uq_result_scenario_variant", ["experiment_id", "scenario_id", "variant_label"]
        )

    with op.batch_alter_table("experiments", schema=None) as batch_op:
        batch_op.add_column(sa.Column("seed", sa.Integer(), server_default="42", nullable=False))
        batch_op.add_column(sa.Column("scenario_limit", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("worker_id", sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("experiments", schema=None) as batch_op:
        batch_op.drop_column("heartbeat_at")
        batch_op.drop_column("worker_id")
        batch_op.drop_column("scenario_limit")
        batch_op.drop_column("seed")

    with op.batch_alter_table("experiment_results", schema=None) as batch_op:
        batch_op.drop_constraint("uq_result_scenario_variant", type_="unique")
