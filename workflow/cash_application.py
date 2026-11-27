"""Cash application: matching incoming bank transactions to open invoices.

Pure functions, like the state machine: a bank line and the open items in, a decision
out. No database and no clock, so every rule is unit-tested and every match can be
explained after the fact from the rule name and reason stored with it.

The rules, in the order a person doing this by hand would try them:

  1. invoice_reference   The narrative names one open invoice (banks often drop the
                         hyphen: "INV100432" matches INV-100432). Applied up to its
                         balance; a payment within the settlement tolerance settles it.
  2. invoice_references  It names several, and the amount is their combined balance.
  3. customer_amount     No invoice named, but the payer is a known customer and the
                         amount is exactly one of their open invoices' balance.
  4. customer_account    ... or exactly their whole open balance: an account payment.

Anything else is an exception for a person, with the reason it did not match and, when
the payer was recognised, which customer it probably belongs to. Auto-applying a
payment to the wrong invoice is worse than not applying it: the customer gets chased
for an invoice they paid, and the one they did not pay is marked settled.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

# Words that differ between how a customer is named in the ledger and how their bank
# names them ("Acme Pvt Ltd" vs "ACME PRIVATE LIMITED").
_NAME_NOISE = frozenset(
    {"pvt", "private", "ltd", "limited", "llp", "inc", "co", "company", "and", "the", "m", "s"}
)


@dataclass(frozen=True)
class BankLine:
    """One credit on the bank statement."""

    utr: str  # the bank's unique transaction reference; the idempotency key
    value_date: date
    amount: Decimal
    currency: str
    reference: str = ""  # the narrative / remittance information
    payer_name: str = ""


@dataclass(frozen=True)
class OpenItem:
    invoice_id: str
    customer_id: str
    customer_name: str
    currency: str
    outstanding: Decimal
    due_date: date


@dataclass(frozen=True)
class Allocation:
    invoice_id: str
    amount: Decimal
    # The allocation leaves a remainder within tolerance, which is written off: the
    # invoice closes.
    settles: bool


@dataclass(frozen=True)
class MatchResult:
    matched: bool
    rule: str
    reason: str
    allocations: tuple[Allocation, ...] = ()
    customer_id: str | None = None  # best guess, also for exceptions
    invoice_ids: tuple[str, ...] = field(default=())  # invoices the narrative named

    @property
    def applied(self) -> Decimal:
        return sum((a.amount for a in self.allocations), Decimal("0"))


def _compact(text: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", text.upper())


def _id_pattern(invoice_id: str) -> re.Pattern[str] | None:
    chars = [c for c in invoice_id.upper() if c.isalnum()]
    if not chars:
        return None
    # Any separators (or none) between the id's characters: "INV-100", "INV100" and
    # "INV 100" are all the same invoice to a person reading the statement.
    body = "[^A-Z0-9]*".join(re.escape(c) for c in chars)
    return re.compile(rf"(?<![0-9]){body}(?![0-9])")


def referenced_invoices(reference: str, invoice_ids: list[str]) -> list[str]:
    """Invoice ids named in a narrative, tolerant of the separators banks strip.

    A digit boundary on both sides keeps INV-1043 from being found inside INV-10432.
    """
    haystack = reference.upper()
    found = []
    for invoice_id in invoice_ids:
        pattern = _id_pattern(invoice_id)
        if pattern and pattern.search(haystack):
            found.append(invoice_id)
    return found


def _name_key(name: str) -> frozenset[str]:
    words = re.findall(r"[a-z0-9]+", name.lower())
    return frozenset(w for w in words if w not in _NAME_NOISE)


def identify_payer(line: BankLine, items: list[OpenItem]) -> str | None:
    """The customer a payment is from: their id in the narrative, or the payer name
    matching their ledger name. None when nobody, or more than one customer, fits."""
    customers = {i.customer_id: i.customer_name for i in items}
    by_id = [c for c in customers if _compact(c) and _compact(c) in _compact(line.reference)]
    if len(by_id) == 1:
        return by_id[0]
    payer = _name_key(line.payer_name)
    if not payer:
        return None
    by_name = [c for c, name in customers.items() if _name_key(name) == payer]
    return by_name[0] if len(by_name) == 1 else None


def _fmt(amount: Decimal, currency: str) -> str:
    return f"{amount:.2f} {currency}"


def match(
    line: BankLine,
    items: list[OpenItem],
    *,
    tolerance: Decimal = Decimal("0"),
    closed_invoice_ids: frozenset[str] = frozenset(),
) -> MatchResult:
    """Decide what one bank line pays. `items` are the open invoices; `closed_invoice_ids`
    lets a payment against an already-settled invoice be flagged as a likely duplicate
    instead of silently going unmatched."""
    currency = line.currency.upper()
    if line.amount <= 0:
        return MatchResult(False, "none", "not a credit")

    named = referenced_invoices(line.reference, [i.invoice_id for i in items])
    if not named:
        closed = referenced_invoices(line.reference, sorted(closed_invoice_ids))
        if closed:
            return MatchResult(
                False,
                "none",
                f"references {', '.join(closed)}, already settled: possible duplicate payment",
                invoice_ids=tuple(closed),
            )

    if named:
        targets = [i for i in items if i.invoice_id in named]
        customer = targets[0].customer_id if len({t.customer_id for t in targets}) == 1 else None
        if any(t.currency.upper() != currency for t in targets):
            return MatchResult(
                False,
                "none",
                f"paid in {currency}, but {', '.join(named)} is billed in another currency",
                customer_id=customer,
                invoice_ids=tuple(named),
            )
        if len(targets) == 1:
            [t] = targets
            # Any excess is the customer's money (a refund or a credit note), so even a
            # small one goes to a person rather than vanishing into the invoice.
            if line.amount > t.outstanding:
                return MatchResult(
                    False,
                    "none",
                    f"overpayment: {_fmt(line.amount, currency)} against a balance of "
                    f"{_fmt(t.outstanding, currency)} on {t.invoice_id}",
                    customer_id=customer,
                    invoice_ids=(t.invoice_id,),
                )
            amount = line.amount
            left = t.outstanding - amount
            settles = left <= tolerance
            return MatchResult(
                True,
                "invoice_reference",
                f"narrative names {t.invoice_id}"
                + ("" if settles else f"; part payment, {_fmt(left, currency)} left"),
                allocations=(Allocation(t.invoice_id, amount, settles),),
                customer_id=customer,
                invoice_ids=(t.invoice_id,),
            )
        total = sum((t.outstanding for t in targets), Decimal("0"))
        if abs(line.amount - total) <= tolerance and line.amount <= total:
            return MatchResult(
                True,
                "invoice_references",
                f"narrative names {len(targets)} invoices and the amount is their total",
                allocations=_settle_all(targets, line.amount),
                customer_id=customer,
                invoice_ids=tuple(named),
            )
        return MatchResult(
            False,
            "none",
            f"names {', '.join(named)} (total {_fmt(total, currency)}) but pays "
            f"{_fmt(line.amount, currency)}: which invoice it is short on is a judgement call",
            customer_id=customer,
            invoice_ids=tuple(named),
        )

    customer = identify_payer(line, items)
    if customer is None:
        return MatchResult(False, "none", "no invoice reference and the payer is not recognised")
    theirs = sorted(
        (i for i in items if i.customer_id == customer and i.currency.upper() == currency),
        key=lambda i: (i.due_date, i.invoice_id),
    )
    if not theirs:
        return MatchResult(
            False, "none", f"no open {currency} invoice for this customer", customer_id=customer
        )
    exact = [i for i in theirs if Decimal("0") <= i.outstanding - line.amount <= tolerance]
    if len(exact) == 1:
        [t] = exact
        return MatchResult(
            True,
            "customer_amount",
            f"payer recognised; amount equals the balance of {t.invoice_id}",
            allocations=(Allocation(t.invoice_id, line.amount, True),),
            customer_id=customer,
        )
    if len(exact) > 1:
        return MatchResult(
            False,
            "none",
            f"amount fits {len(exact)} of the customer's invoices equally: "
            f"{', '.join(i.invoice_id for i in exact)}",
            customer_id=customer,
        )
    total = sum((i.outstanding for i in theirs), Decimal("0"))
    if len(theirs) > 1 and Decimal("0") <= total - line.amount <= tolerance:
        return MatchResult(
            True,
            "customer_account",
            f"payer recognised; amount clears their whole open balance ({len(theirs)} invoices)",
            allocations=_settle_all(theirs, line.amount),
            customer_id=customer,
        )
    return MatchResult(
        False,
        "none",
        f"payer recognised, but {_fmt(line.amount, currency)} matches no open invoice "
        f"and not the account total of {_fmt(total, currency)}",
        customer_id=customer,
    )


def _settle_all(targets: list[OpenItem], amount: Decimal) -> tuple[Allocation, ...]:
    """Settle every target in full; any shortfall (within tolerance) comes off the
    last, so each invoice is either paid or within tolerance of it."""
    ordered = sorted(targets, key=lambda t: (t.due_date, t.invoice_id))
    remaining = amount
    allocations = []
    for t in ordered:
        share = min(t.outstanding, remaining)
        allocations.append(Allocation(t.invoice_id, share, True))
        remaining -= share
    return tuple(allocations)
