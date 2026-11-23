"""baseline schema

The schema as it stood when migrations were introduced, generated from that commit's
models. Databases created earlier by `Base.metadata.create_all` already match it, so
init_db() stamps them at this revision instead of re-creating their tables.

Revision ID: 0001
Revises:
Create Date: 2026-11-23 18:11:04.391767
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "call_logs",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("invoice_id", sa.String(length=64), nullable=False),
        sa.Column("customer_id", sa.String(length=64), nullable=False),
        sa.Column("prompt_id", sa.String(length=128), nullable=False),
        sa.Column("risk_tier", sa.String(length=16), nullable=True),
        sa.Column("mode", sa.String(length=16), nullable=False),
        sa.Column("transcript", sa.Text(), nullable=False),
        sa.Column("outcome", sa.String(length=32), nullable=True),
        sa.Column("promise_to_pay_date", sa.String(length=10), nullable=True),
        sa.Column("outcome_confidence", sa.Float(), nullable=False),
        sa.Column("scores", sa.JSON(), nullable=False),
        sa.Column("composite", sa.Float(), nullable=False),
        sa.Column("hallucinated", sa.Boolean(), nullable=False),
        sa.Column("hallucinated_figures", sa.JSON(), nullable=False),
        sa.Column("compliance_violations", sa.JSON(), nullable=False),
        sa.Column("duration_ms", sa.Float(), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("call_logs", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_call_logs_created_at"), ["created_at"], unique=False)
        batch_op.create_index(batch_op.f("ix_call_logs_customer_id"), ["customer_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_call_logs_invoice_id"), ["invoice_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_call_logs_prompt_id"), ["prompt_id"], unique=False)

    op.create_table(
        "experiments",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("variant_a_id", sa.String(length=128), nullable=False),
        sa.Column("variant_b_id", sa.String(length=128), nullable=False),
        sa.Column("eval_set", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("scenario_count", sa.Integer(), nullable=False),
        sa.Column("completed_count", sa.Integer(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("experiments", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_experiments_status"), ["status"], unique=False)

    op.create_table(
        "prompt_versions",
        sa.Column("id", sa.String(length=128), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("tone", sa.String(length=32), nullable=False),
        sa.Column("template", sa.Text(), nullable=False),
        sa.Column("changelog", sa.Text(), nullable=False),
        sa.Column("applies_to_tier", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name", "version", name="uq_prompt_name_version"),
    )
    with op.batch_alter_table("prompt_versions", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_prompt_versions_name"), ["name"], unique=False)

    op.create_table(
        "experiment_results",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("experiment_id", sa.String(length=32), nullable=False),
        sa.Column("scenario_id", sa.String(length=128), nullable=False),
        sa.Column("variant_label", sa.String(length=1), nullable=False),
        sa.Column("prompt_id", sa.String(length=128), nullable=False),
        sa.Column("transcript", sa.Text(), nullable=False),
        sa.Column("scores", sa.JSON(), nullable=False),
        sa.Column("composite", sa.Float(), nullable=False),
        sa.Column("hallucinated", sa.Boolean(), nullable=False),
        sa.Column("hallucinated_figures", sa.JSON(), nullable=False),
        sa.Column("compliance_violations", sa.JSON(), nullable=False),
        sa.Column("judge_rationale", sa.Text(), nullable=False),
        sa.Column("extracted_outcome", sa.String(length=32), nullable=True),
        sa.Column("expected_outcome", sa.String(length=32), nullable=True),
        sa.Column("outcome_match", sa.Boolean(), nullable=False),
        sa.Column("promise_date_match", sa.Boolean(), nullable=True),
        sa.Column("total_latency_ms", sa.Float(), nullable=False),
        sa.Column("mean_turn_latency_ms", sa.Float(), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("is_adversarial", sa.Boolean(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("pairwise_winner", sa.String(length=4), nullable=True),
        sa.Column("pairwise_reason", sa.Text(), nullable=False),
        sa.Column("presented_a_first", sa.Boolean(), nullable=True),
        sa.ForeignKeyConstraint(["experiment_id"], ["experiments.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("experiment_results", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_experiment_results_experiment_id"), ["experiment_id"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_experiment_results_prompt_id"), ["prompt_id"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_experiment_results_scenario_id"), ["scenario_id"], unique=False
        )


def downgrade() -> None:
    with op.batch_alter_table("experiment_results", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_experiment_results_scenario_id"))
        batch_op.drop_index(batch_op.f("ix_experiment_results_prompt_id"))
        batch_op.drop_index(batch_op.f("ix_experiment_results_experiment_id"))

    op.drop_table("experiment_results")
    with op.batch_alter_table("prompt_versions", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_prompt_versions_name"))

    op.drop_table("prompt_versions")
    with op.batch_alter_table("experiments", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_experiments_status"))

    op.drop_table("experiments")
    with op.batch_alter_table("call_logs", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_call_logs_prompt_id"))
        batch_op.drop_index(batch_op.f("ix_call_logs_invoice_id"))
        batch_op.drop_index(batch_op.f("ix_call_logs_customer_id"))
        batch_op.drop_index(batch_op.f("ix_call_logs_created_at"))

    op.drop_table("call_logs")
