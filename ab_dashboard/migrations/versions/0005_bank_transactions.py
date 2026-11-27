"""bank transactions

Incoming bank credits and what cash application did with each: matched to invoices
automatically, waiting in the exceptions queue, applied by a person, or dismissed. The
bank reference (UTR) is unique so a replayed feed cannot apply the same money twice.

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-27 21:36:56.292954
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "bank_transactions",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("utr", sa.String(length=64), nullable=False),
        sa.Column("value_date", sa.Date(), nullable=False),
        sa.Column("amount", sa.String(length=32), nullable=False),
        sa.Column("currency", sa.String(length=8), nullable=False),
        sa.Column("reference", sa.Text(), nullable=False),
        sa.Column("payer_name", sa.String(length=255), nullable=False),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("match_rule", sa.String(length=32), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("customer_id", sa.String(length=64), nullable=True),
        sa.Column("allocations", sa.JSON(), nullable=False),
        sa.Column("resolution_note", sa.Text(), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("utr"),
    )
    with op.batch_alter_table("bank_transactions", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_bank_transactions_customer_id"), ["customer_id"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_bank_transactions_received_at"), ["received_at"], unique=False
        )
        batch_op.create_index(batch_op.f("ix_bank_transactions_status"), ["status"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("bank_transactions", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_bank_transactions_status"))
        batch_op.drop_index(batch_op.f("ix_bank_transactions_received_at"))
        batch_op.drop_index(batch_op.f("ix_bank_transactions_customer_id"))
    op.drop_table("bank_transactions")
