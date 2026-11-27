"""A simulated bank feed, for demos and tests.

There is no real bank behind this project, so this stands in for one: given the open
invoices, it produces the kind of credits a real AR team sees in a day. Most are clean,
some are messy in the ways real ones are, and a few cannot be matched by any rule - so a
single run shows both halves of cash application: invoices closing on their own, and
the exceptions queue a person works.

Pure: open items and a seed in, bank lines out.
"""

from __future__ import annotations

import random
from collections import defaultdict
from datetime import date
from decimal import Decimal

from workflow.cash_application import BankLine, OpenItem

# The order kinds are drawn in: a run of 8 shows one of each, then clean ones repeat.
KINDS = (
    "clean",  # full balance, invoice number in the narrative
    "account",  # whole account balance, no invoice number, payer name only
    "part",  # part payment against an invoice number
    "bank_charges",  # full balance less a transfer fee deducted in transit
    "no_separator",  # the bank stripped the hyphen: "INV100432"
    "unknown_payer",  # no invoice number, a payer the ledger has never heard of
    "overpayment",  # more than the balance: someone has to refund or credit it
    "clean",
)


def _utr(rng: random.Random, today: date) -> str:
    return f"SIM{today:%y%m%d}{rng.randrange(10**8):08d}"


def _narrative(rng: random.Random, invoice_id: str, payer: str) -> str:
    rail = rng.choice(["NEFT", "RTGS", "IMPS", "UPI"])
    return f"{rail}/{invoice_id}/{payer.upper()[:14]}"


def simulate_feed(
    items: list[OpenItem], *, today: date, count: int = 8, seed: int | None = None
) -> list[BankLine]:
    """Up to `count` credits drawn from `items`, each invoice used at most once."""
    rng = random.Random(seed)
    pool = sorted(items, key=lambda i: i.invoice_id)
    rng.shuffle(pool)
    by_customer: dict[tuple[str, str], list[OpenItem]] = defaultdict(list)
    for item in pool:
        by_customer[(item.customer_id, item.currency)].append(item)
    used: set[str] = set()
    lines: list[BankLine] = []
    # Held back from single-invoice payments, so the account payment can clear it whole.
    account = next((g for g in by_customer.values() if len(g) > 1), None)
    reserved = {i.invoice_id for i in account} if account else set()

    def take() -> OpenItem | None:
        for item in pool:
            if item.invoice_id not in used | reserved and item.outstanding > 100:
                used.add(item.invoice_id)
                return item
        return None

    def line(amount: Decimal, currency: str, reference: str, payer: str) -> BankLine:
        return BankLine(
            utr=_utr(rng, today),
            value_date=today,
            amount=amount.quantize(Decimal("0.01")),
            currency=currency,
            reference=reference,
            payer_name=payer,
        )

    for n in range(count):
        kind = KINDS[n % len(KINDS)]
        if kind == "unknown_payer":
            amount = Decimal(rng.randrange(5_000, 90_000))
            lines.append(line(amount, "INR", "FUND TRANSFER", "R K ENTERPRISES"))
            continue
        if kind == "account":
            if account and not reserved & used:
                used.update(reserved)
                total = sum((i.outstanding for i in account), Decimal("0"))
                payer = account[0].customer_name.upper()
                lines.append(line(total, account[0].currency, "PAYMENT OF DUES", payer))
                continue
            kind = "clean"
        item = take()
        if item is None:
            break
        payer = item.customer_name
        ref = _narrative(rng, item.invoice_id, payer)
        if kind == "part":
            amount = (item.outstanding * Decimal("0.4")).quantize(Decimal("1"))
        elif kind == "bank_charges":
            fee = Decimal("25") if item.currency == "INR" else Decimal("0.50")
            amount = item.outstanding - fee
        elif kind == "no_separator":
            amount = item.outstanding
            ref = f"IMPS {item.invoice_id.replace('-', '')}"
        elif kind == "overpayment":
            amount = item.outstanding + Decimal("500")
        else:
            amount = item.outstanding
        lines.append(line(amount, item.currency, ref, payer))
    return lines
