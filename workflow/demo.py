"""Put a public demo back to a clean state.

Visitors with the operator role can record outcomes, apply payments and close tasks, so a
demo drifts. This wipes the workflow and cash application (invoice states, contacts,
tasks, the audit trail, bank transactions) and reloads the ledger, as if the app had just
been deployed. Prompt versions and experiment results are kept: they are slow and costly
to recreate and visitors cannot change them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import delete

from ab_dashboard.db import (
    BankTransactionRow,
    ContactAttemptRow,
    InvoiceEventRow,
    InvoiceStateRow,
    TaskRow,
    get_sessionmaker,
)
from workflow import service

# Children before the invoice states they reference, so this works with or without the
# database enforcing ON DELETE CASCADE.
TABLES = (InvoiceEventRow, TaskRow, ContactAttemptRow, BankTransactionRow, InvoiceStateRow)


@dataclass
class ResetReport:
    deleted: dict[str, int] = field(default_factory=dict)
    reloaded: service.SyncReport | None = None


async def reset(*, reload: bool = True, now: datetime | None = None) -> ResetReport:
    report = ResetReport()
    async with get_sessionmaker()() as session, session.begin():
        for model in TABLES:
            result = await session.execute(delete(model))
            report.deleted[model.__tablename__] = result.rowcount or 0
    if reload:
        from data.generate_dataset import load_invoices

        report.reloaded = await service.sync_invoices(load_invoices(), now=now)
    return report
