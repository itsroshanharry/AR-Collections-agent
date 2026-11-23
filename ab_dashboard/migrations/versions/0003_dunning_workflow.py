"""dunning workflow

Per-invoice dunning state (with an optimistic-lock version column), the contact history
the frequency caps are computed from, follow-up tasks (unique dedupe_key for idempotent
creation), and the append-only audit trail of every transition.

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-23 18:44:32.939819
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "invoice_states",
        sa.Column("invoice_id", sa.String(length=64), nullable=False),
        sa.Column("customer_id", sa.String(length=64), nullable=False),
        sa.Column("invoice", sa.JSON(), nullable=False),
        sa.Column("amount_outstanding", sa.String(length=32), nullable=False),
        sa.Column("timezone", sa.String(length=64), nullable=True),
        sa.Column("do_not_call", sa.Boolean(), nullable=False),
        sa.Column("state", sa.String(length=24), nullable=False),
        sa.Column("tier", sa.String(length=16), nullable=False),
        sa.Column("cadence_tier", sa.String(length=16), nullable=False),
        sa.Column("cadence_step", sa.Integer(), nullable=False),
        sa.Column("cadence_started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_contact_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("promise_to_pay_date", sa.Date(), nullable=True),
        sa.Column("broken_promises", sa.Integer(), nullable=False),
        sa.Column("escalations", sa.Integer(), nullable=False),
        sa.Column("closed_reason", sa.String(length=32), nullable=True),
        sa.Column("next_action_channel", sa.String(length=16), nullable=True),
        sa.Column("next_action_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("snapshot_days_overdue", sa.Integer(), nullable=False),
        sa.Column("synced_on", sa.Date(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("invoice_id"),
    )
    with op.batch_alter_table("invoice_states", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_invoice_states_customer_id"), ["customer_id"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_invoice_states_next_action_at"), ["next_action_at"], unique=False
        )
        batch_op.create_index(batch_op.f("ix_invoice_states_state"), ["state"], unique=False)

    op.create_table(
        "contact_attempts",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("invoice_id", sa.String(length=64), nullable=False),
        sa.Column("customer_id", sa.String(length=64), nullable=False),
        sa.Column("channel", sa.String(length=16), nullable=False),
        sa.Column("attempted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("outcome", sa.String(length=32), nullable=True),
        sa.Column("call_log_id", sa.String(length=32), nullable=True),
        sa.ForeignKeyConstraint(["invoice_id"], ["invoice_states.invoice_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("contact_attempts", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_contact_attempts_attempted_at"), ["attempted_at"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_contact_attempts_customer_id"), ["customer_id"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_contact_attempts_invoice_id"), ["invoice_id"], unique=False
        )

    op.create_table(
        "invoice_events",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("invoice_id", sa.String(length=64), nullable=False),
        sa.Column("event", sa.String(length=64), nullable=False),
        sa.Column("from_state", sa.String(length=24), nullable=False),
        sa.Column("to_state", sa.String(length=24), nullable=False),
        sa.Column("tier", sa.String(length=16), nullable=False),
        sa.Column("detail", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["invoice_id"], ["invoice_states.invoice_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("invoice_events", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_invoice_events_created_at"), ["created_at"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_invoice_events_invoice_id"), ["invoice_id"], unique=False
        )

    op.create_table(
        "tasks",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("invoice_id", sa.String(length=64), nullable=False),
        sa.Column("task_type", sa.String(length=32), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("assignee", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("due_date", sa.Date(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("dedupe_key", sa.String(length=160), nullable=False),
        sa.Column("call_log_id", sa.String(length=32), nullable=True),
        sa.Column("resolution", sa.String(length=32), nullable=True),
        sa.Column("resolution_note", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["invoice_id"], ["invoice_states.invoice_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("dedupe_key"),
    )
    with op.batch_alter_table("tasks", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_tasks_assignee"), ["assignee"], unique=False)
        batch_op.create_index(batch_op.f("ix_tasks_due_date"), ["due_date"], unique=False)
        batch_op.create_index(batch_op.f("ix_tasks_invoice_id"), ["invoice_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_tasks_status"), ["status"], unique=False)
        batch_op.create_index(batch_op.f("ix_tasks_task_type"), ["task_type"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("tasks", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_tasks_task_type"))
        batch_op.drop_index(batch_op.f("ix_tasks_status"))
        batch_op.drop_index(batch_op.f("ix_tasks_invoice_id"))
        batch_op.drop_index(batch_op.f("ix_tasks_due_date"))
        batch_op.drop_index(batch_op.f("ix_tasks_assignee"))

    op.drop_table("tasks")
    with op.batch_alter_table("invoice_events", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_invoice_events_invoice_id"))
        batch_op.drop_index(batch_op.f("ix_invoice_events_created_at"))

    op.drop_table("invoice_events")
    with op.batch_alter_table("contact_attempts", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_contact_attempts_invoice_id"))
        batch_op.drop_index(batch_op.f("ix_contact_attempts_customer_id"))
        batch_op.drop_index(batch_op.f("ix_contact_attempts_attempted_at"))

    op.drop_table("contact_attempts")
    with op.batch_alter_table("invoice_states", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_invoice_states_state"))
        batch_op.drop_index(batch_op.f("ix_invoice_states_next_action_at"))
        batch_op.drop_index(batch_op.f("ix_invoice_states_customer_id"))

    op.drop_table("invoice_states")
