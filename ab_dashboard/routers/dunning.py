"""Dunning workflow API: invoice state, the call queue, payments and follow-up tasks."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, Field

from ab_dashboard.db import ContactAttemptRow, InvoiceEventRow, InvoiceStateRow, TaskRow
from shared.models import AccountItem, CallOutcome, OutcomeType
from workflow import service
from workflow.policy import TaskType
from workflow.state_machine import RESOLUTIONS, TransitionError

router = APIRouter(prefix="/dunning", tags=["dunning"])
tasks_router = APIRouter(prefix="/tasks", tags=["tasks"])


# --------------------------------------------------------------------------------------
# Schemas
# --------------------------------------------------------------------------------------


class InvoiceStateOut(BaseModel):
    invoice_id: str
    customer_id: str
    customer_name: str
    currency: str
    amount_outstanding: str
    state: str
    tier: str
    cadence_tier: str
    cadence_step: int
    next_action_channel: str | None
    next_action_at: datetime | None
    last_contact_at: datetime | None
    promise_to_pay_date: date | None
    broken_promises: int
    do_not_call: bool
    closed_reason: str | None


class TaskOut(BaseModel):
    id: str
    invoice_id: str
    task_type: str
    title: str
    assignee: str
    status: str
    due_date: date
    payload: dict
    resolution: str | None
    resolution_note: str
    allowed_resolutions: list[str]
    created_at: datetime
    resolved_at: datetime | None


class EventOut(BaseModel):
    event: str
    from_state: str
    to_state: str
    tier: str
    detail: dict
    created_at: datetime


class AttemptOut(BaseModel):
    channel: str
    attempted_at: datetime
    outcome: str | None
    call_log_id: str | None


class AccountItemOut(BaseModel):
    invoice_id: str
    amount_outstanding: str
    currency: str
    due_date: date
    days_overdue: int


class InvoiceDetailOut(BaseModel):
    state: InvoiceStateOut
    days_overdue_today: int
    # The customer's other invoices in active dunning: what a call about this one covers.
    other_open_invoices: list[AccountItemOut]
    tasks: list[TaskOut]
    contact_attempts: list[AttemptOut]
    timeline: list[EventOut]


class DueCallOut(BaseModel):
    invoice_id: str
    customer_id: str
    customer_name: str
    tier: str
    priority: float
    days_overdue: int
    amount_outstanding: str
    currency: str
    broken_promises: int
    # One call per customer: the rest of the account it covers, and the total to ask for.
    other_open_invoices: list[AccountItemOut]
    account_total: dict[str, str]


class CustomerInvoiceOut(InvoiceStateOut):
    days_overdue_today: int


class CustomerAccountOut(BaseModel):
    customer_id: str
    customer_name: str
    overdue_in_dunning: dict[str, str]
    outstanding: dict[str, str]
    invoices: list[CustomerInvoiceOut]
    open_tasks: list[TaskOut]


class OutcomeIn(BaseModel):
    """A call outcome recorded by hand (a person's call, or a demo without audio)."""

    outcome: OutcomeType
    promise_to_pay_date: date | None = None
    promised_amount: Decimal | None = None
    dispute_reason: str | None = None
    payment_reference: str | None = None
    note: str = ""
    # The call discussed the customer's whole account, not only this invoice. A promise
    # for the account total then applies to every invoice they have in dunning.
    covers_account: bool = False


class OutcomeOut(BaseModel):
    invoice_id: str
    state: str
    tier: str
    tasks_opened: list[str]
    discrepancy: str | None
    next_action_channel: str | None
    next_action_at: datetime | None
    account_invoices: list[str]
    promise_covers_account: bool


class PaymentIn(BaseModel):
    amount: Decimal = Field(gt=0)
    reference: str = ""


class DoNotCallIn(BaseModel):
    do_not_call: bool


class TaskResolveIn(BaseModel):
    resolution: str = Field(examples=["received"])
    note: str = ""


def _state_out(row: InvoiceStateRow) -> InvoiceStateOut:
    return InvoiceStateOut(
        invoice_id=row.invoice_id,
        customer_id=row.customer_id,
        customer_name=row.invoice.get("customer", {}).get("name", ""),
        currency=row.invoice.get("currency", ""),
        amount_outstanding=row.amount_outstanding,
        state=row.state,
        tier=row.tier,
        cadence_tier=row.cadence_tier,
        cadence_step=row.cadence_step,
        next_action_channel=row.next_action_channel,
        next_action_at=row.next_action_at,
        last_contact_at=row.last_contact_at,
        promise_to_pay_date=row.promise_to_pay_date,
        broken_promises=row.broken_promises,
        do_not_call=row.do_not_call,
        closed_reason=row.closed_reason,
    )


def _item_out(item: AccountItem) -> AccountItemOut:
    return AccountItemOut(
        invoice_id=item.invoice_id,
        amount_outstanding=f"{item.amount:.2f}",
        currency=item.currency,
        due_date=item.due_date,
        days_overdue=item.days_overdue,
    )


def _money(totals: dict[str, Decimal]) -> dict[str, str]:
    return {currency: f"{amount:.2f}" for currency, amount in totals.items()}


def _task_out(task: TaskRow) -> TaskOut:
    return TaskOut(
        id=task.id,
        invoice_id=task.invoice_id,
        task_type=task.task_type,
        title=task.title,
        assignee=task.assignee,
        status=task.status,
        due_date=task.due_date,
        payload=task.payload or {},
        resolution=task.resolution,
        resolution_note=task.resolution_note,
        allowed_resolutions=sorted(RESOLUTIONS[TaskType(task.task_type)]),
        created_at=task.created_at,
        resolved_at=task.resolved_at,
    )


def _event_out(event: InvoiceEventRow) -> EventOut:
    return EventOut(
        event=event.event,
        from_state=event.from_state,
        to_state=event.to_state,
        tier=event.tier,
        detail=event.detail or {},
        created_at=event.created_at,
    )


def _attempt_out(attempt: ContactAttemptRow) -> AttemptOut:
    return AttemptOut(
        channel=attempt.channel,
        attempted_at=attempt.attempted_at,
        outcome=attempt.outcome,
        call_log_id=attempt.call_log_id,
    )


def _raise_for(exc: Exception) -> None:
    if isinstance(exc, service.NotFoundError):
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    if isinstance(exc, TransitionError | ValueError):
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    if isinstance(exc, service.ConcurrentUpdateError):
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    raise exc


# --------------------------------------------------------------------------------------
# Workflow
# --------------------------------------------------------------------------------------


@router.post("/sync", response_model=service.SyncReport)
async def sync(limit: int | None = Query(default=None, ge=1)) -> service.SyncReport:
    """Load the ledger (the generated AR dataset) into the workflow. Idempotent."""
    from data.generate_dataset import load_invoices

    try:
        invoices = load_invoices()
    except FileNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    return await service.sync_invoices(invoices[:limit] if limit else invoices)


@router.post("/tick", response_model=service.TickReport)
async def tick() -> service.TickReport:
    """Run one scheduler pass now. The app also runs this on a timer."""
    return await service.tick()


@router.get("/queue", response_model=list[DueCallOut])
async def call_queue(limit: int = Query(default=20, ge=1, le=200)) -> list[DueCallOut]:
    """Calls the playbook wants right now and the contact rules permit, by priority.
    One per customer, filed under their most urgent invoice and covering the rest."""
    return [
        DueCallOut(
            invoice_id=d.invoice.invoice_id,
            customer_id=d.invoice.customer.customer_id,
            customer_name=d.invoice.customer.name,
            tier=d.tier.value,
            priority=d.priority,
            days_overdue=d.invoice.days_overdue,
            amount_outstanding=f"{d.invoice.amount:.2f}",
            currency=d.invoice.currency,
            broken_promises=d.broken_promises,
            other_open_invoices=[_item_out(i) for i in d.invoice.other_open_invoices],
            account_total=_money(d.invoice.account_totals()),
        )
        for d in await service.due_calls(limit=limit)
    ]


@router.get("/customers/{customer_id}", response_model=CustomerAccountOut)
async def get_customer(customer_id: str) -> CustomerAccountOut:
    """The customer's whole account: every invoice, the balance in dunning, open work."""
    try:
        account = await service.customer_account(customer_id)
    except Exception as exc:  # noqa: BLE001
        _raise_for(exc)
    return CustomerAccountOut(
        customer_id=account.customer_id,
        customer_name=account.customer_name,
        overdue_in_dunning=_money(account.overdue_in_dunning),
        outstanding=_money(account.outstanding),
        invoices=[
            CustomerInvoiceOut(
                **_state_out(row).model_dump(), days_overdue_today=inv.days_overdue
            )
            for row, inv in account.invoices
        ],
        open_tasks=[_task_out(t) for t in account.open_tasks],
    )


@router.get("/invoices/{invoice_id}", response_model=InvoiceDetailOut)
async def get_invoice(invoice_id: str) -> InvoiceDetailOut:
    """Current state, open and closed tasks, every contact, and the full audit trail."""
    try:
        detail = await service.invoice_detail(invoice_id)
    except Exception as exc:  # noqa: BLE001
        _raise_for(exc)
    return InvoiceDetailOut(
        state=_state_out(detail.row),
        days_overdue_today=detail.invoice.days_overdue,
        other_open_invoices=[_item_out(i) for i in detail.invoice.other_open_invoices],
        tasks=[_task_out(t) for t in detail.tasks],
        contact_attempts=[_attempt_out(a) for a in detail.attempts],
        timeline=[_event_out(e) for e in detail.events],
    )


@router.post("/invoices/{invoice_id}/outcomes", response_model=OutcomeOut)
async def record_outcome(invoice_id: str, payload: OutcomeIn) -> OutcomeOut:
    """Record a call outcome by hand. The voice agent records its own automatically."""
    outcome = CallOutcome(
        outcome=payload.outcome,
        promise_to_pay_date=payload.promise_to_pay_date,
        promised_amount=payload.promised_amount,
        dispute_reason=payload.dispute_reason,
        confidence=1.0,
        notes=payload.note,
    )
    captured = (
        {"payment_reference": payload.payment_reference} if payload.payment_reference else None
    )
    try:
        report = await service.record_call_outcome(
            invoice_id, outcome, captured=captured, cover_account=payload.covers_account
        )
    except Exception as exc:  # noqa: BLE001
        _raise_for(exc)
    return OutcomeOut(**report.__dict__)


@router.post("/invoices/{invoice_id}/payments", response_model=InvoiceStateOut)
async def record_payment(invoice_id: str, payload: PaymentIn) -> InvoiceStateOut:
    """Apply a payment. Stands in for the ERP or bank feed a real deployment would use."""
    try:
        row = await service.record_payment(
            invoice_id, payload.amount, reference=payload.reference
        )
    except Exception as exc:  # noqa: BLE001
        _raise_for(exc)
    return _state_out(row)


@router.patch("/invoices/{invoice_id}", response_model=InvoiceStateOut)
async def update_invoice(invoice_id: str, payload: DoNotCallIn) -> InvoiceStateOut:
    """Set or clear do-not-call. Calls and SMS in the cadence then go out as email."""
    try:
        row = await service.set_do_not_call(invoice_id, payload.do_not_call)
    except Exception as exc:  # noqa: BLE001
        _raise_for(exc)
    return _state_out(row)


# --------------------------------------------------------------------------------------
# Tasks
# --------------------------------------------------------------------------------------


@tasks_router.get("", response_model=list[TaskOut])
async def list_tasks(
    assignee: str | None = Query(default=None, examples=["disputes"]),
    status_: str | None = Query(default="open", alias="status"),
    task_type: TaskType | None = None,
    invoice_id: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
) -> list[TaskOut]:
    """A team's work queue, soonest due first. Pass status= (empty) for all statuses."""
    rows = await service.list_tasks(
        assignee=assignee,
        status=status_ or None,
        task_type=task_type.value if task_type else None,
        invoice_id=invoice_id,
        limit=limit,
    )
    return [_task_out(t) for t in rows]


@tasks_router.patch("/{task_id}", response_model=TaskOut)
async def resolve_task(task_id: str, payload: TaskResolveIn) -> TaskOut:
    """Resolve a task. The resolution moves the invoice on; see allowed_resolutions."""
    try:
        task, _ = await service.resolve_task(task_id, payload.resolution, note=payload.note)
    except Exception as exc:  # noqa: BLE001
        _raise_for(exc)
    return _task_out(task)
