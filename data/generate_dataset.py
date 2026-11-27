"""Build the enriched AR dataset that drives every simulated call.

Public invoice datasets give realistic amount and ageing distributions but never carry
AR-collections fields - GSTIN, HSN/SAC, dispute flags, reminder history, promise-to-pay
outcomes. So we take the Kaggle data's statistical shape as the base and enrich each row
synthetically on top.

    uv run python -m data.generate_dataset --rows 500 --seed 42

The Kaggle base is licence-restricted and gitignored, so it is machine-local. When
data/raw/ is empty this falls back to built-in distributions with a loud warning rather
than failing - a fresh clone or CI run still produces a usable dataset. Output is
byte-identical for a fixed --seed either way.
"""

from __future__ import annotations

import argparse
import csv
import logging
import random
import sys
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from faker import Faker

from shared.config import RAW_DIR, SYNTHETIC_DIR
from shared.models import Customer, Invoice
from voice_agent.dunning_logic import classify_risk

logger = logging.getLogger(__name__)

DEFAULT_OUTPUT = SYNTHETIC_DIR / "ar_dataset_enriched.csv"

# Service-sector HSN/SAC codes plausible for B2B invoicing.
HSN_SAC_CODES = ["998314", "998313", "996511", "998311", "997212"]
CURRENCIES = ["INR", "INR", "INR", "USD", "SGD"]  # weighted toward INR
# Invoices are spread round-robin over this many customers: 500 rows gives each customer
# two or three, which is what makes collections an account-level job.
CUSTOMER_COUNT = 180
DISPUTE_RATE = 0.08

# Fallback ageing distribution (days overdue) when no Kaggle base is present. Shaped
# like a real AR ledger: most invoices current-to-30, a long thin tail of bad debt.
FALLBACK_AGEING = [
    ((0, 15), 0.34),
    ((16, 45), 0.31),
    ((46, 90), 0.20),
    ((91, 180), 0.11),
    ((181, 365), 0.04),
]
FALLBACK_AMOUNT_RANGE = (5_000, 850_000)

FIELDNAMES = [
    "invoice_id",
    "customer_id",
    "customer_name",
    "contact_name",
    "phone",
    "email",
    "gstin",
    "hsn_sac_code",
    "amount",
    "currency",
    "invoice_date",
    "due_date",
    "days_overdue",
    "dispute_flag",
    "reminder_count",
    "last_reminder_date",
    "promise_to_pay_date",
    "risk_tier",
]


def find_base_csv(explicit: Path | None = None) -> Path | None:
    """Locate the Kaggle base CSV, largest first (the main table, not a lookup)."""
    if explicit:
        if not explicit.exists():
            raise FileNotFoundError(f"--base-csv not found: {explicit}")
        return explicit
    candidates = sorted(RAW_DIR.glob("**/*.csv"), key=lambda p: p.stat().st_size, reverse=True)
    return candidates[0] if candidates else None


def _pick_column(header: list[str], *needles: str) -> str | None:
    """Kaggle invoice datasets vary in column naming; match loosely."""
    lowered = {h.lower().replace("_", "").replace(" ", ""): h for h in header}
    for needle in needles:
        key = needle.lower().replace("_", "").replace(" ", "")
        for norm, original in lowered.items():
            if key in norm:
                return original
    return None


def load_base_distributions(csv_path: Path) -> tuple[list[float], list[int]] | None:
    """Extract amount and days-overdue samples from the Kaggle base.

    Returns None if the file has no recognisable amount column, in which case the
    caller falls back to built-in distributions.
    """
    try:
        with csv_path.open(newline="", encoding="utf-8", errors="replace") as fh:
            reader = csv.DictReader(fh)
            if not reader.fieldnames:
                return None
            amount_col = _pick_column(
                reader.fieldnames, "totalopenamount", "openamount", "amount", "invoiceamount"
            )
            due_col = _pick_column(reader.fieldnames, "duein", "duedate", "due")
            clear_col = _pick_column(reader.fieldnames, "cleardate", "paymentdate", "clearing")
            if not amount_col:
                logger.warning("No amount column found in %s; using fallback", csv_path.name)
                return None

            amounts: list[float] = []
            lateness: list[int] = []
            for row in reader:
                try:
                    value = float(str(row[amount_col]).replace(",", "").strip())
                    if 100 <= value <= 5_000_000:
                        amounts.append(value)
                except (ValueError, TypeError, KeyError):
                    pass
                if due_col and clear_col:
                    delta = _days_between(row.get(due_col), row.get(clear_col))
                    # Strictly-late only. The Kaggle base is a payment-date-prediction
                    # set covering every invoice, so ~33% cleared on or before the due
                    # date. Keeping those would (a) give a third of generated rows
                    # days_overdue=0, which build_call_queue then drops entirely, and
                    # (b) leave the high/critical dunning paths almost unexercised.
                    # Conditioning on "given it is late, how late?" is the population a
                    # collections agent actually works.
                    if delta is not None and 1 <= delta <= 365:
                        lateness.append(delta)

            if not amounts:
                return None
            logger.info(
                "Calibrated from %s: %d amounts, %d lateness observations",
                csv_path.name,
                len(amounts),
                len(lateness),
            )
            return amounts, lateness
    except OSError as exc:
        logger.warning("Could not read %s (%s); using fallback", csv_path, exc)
        return None


# Public invoice CSVs are inconsistent about dates. This base alone mixes
# "2020-02-11 00:00:00" (timestamp) with "20200210.0" (YYYYMMDD that went through a
# float column on export). Both are handled explicitly rather than left to chance.
_DATE_FORMATS = ("%Y-%m-%d", "%Y%m%d", "%d-%m-%Y", "%m/%d/%Y", "%d/%m/%Y", "%d-%b-%y")


def _parse_date(raw: str | None) -> date | None:
    if not raw:
        return None
    value = str(raw).strip()
    if not value or value.lower() in {"nan", "nat", "none", "null"}:
        return None
    # Drop a time component ("2020-02-11 00:00:00") and any float tail ("20200210.0").
    value = value.split(" ")[0].split("T")[0]
    if value.endswith(".0"):
        value = value[:-2]
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    return None


def _days_between(due: str | None, cleared: str | None) -> int | None:
    due_date, cleared_date = _parse_date(due), _parse_date(cleared)
    if due_date is None or cleared_date is None:
        return None
    return (cleared_date - due_date).days


def _sample_days_overdue(rng: random.Random, lateness: list[int]) -> int:
    if lateness:
        return rng.choice(lateness)
    (lo, hi), _ = _weighted_choice(rng, FALLBACK_AGEING)
    return rng.randint(lo, hi)


def _weighted_choice(rng: random.Random, weighted: list[tuple[tuple[int, int], float]]):
    roll = rng.random()
    cumulative = 0.0
    for bucket, weight in weighted:
        cumulative += weight
        if roll <= cumulative:
            return bucket, weight
    return weighted[-1]


def _sample_amount(rng: random.Random, amounts: list[float]) -> Decimal:
    value = rng.choice(amounts) if amounts else rng.uniform(*FALLBACK_AMOUNT_RANGE)
    return Decimal(f"{round(value, 2):.2f}")


def _fake_gstin(fake: Faker) -> str:
    """GSTIN shape: 2-digit state code + 10-char PAN + entity digit + 'Z' + checksum."""
    return fake.bothify(text="##?????####?#Z?", letters="ABCDEFGHIJKLMNOPQRSTUVWXYZ").upper()


def generate(
    rows: int,
    seed: int,
    base_csv: Path | None,
    as_of: date,
) -> list[Invoice]:
    rng = random.Random(seed)
    fake = Faker("en_IN")
    Faker.seed(seed)

    base = load_base_distributions(base_csv) if base_csv else None
    if base:
        amounts, lateness = base
    else:
        amounts, lateness = [], []
        logger.warning(
            "No Kaggle base data in %s - falling back to built-in distributions. "
            "For real-shaped amounts, download "
            "pradumn203/payment-date-prediction-for-invoices-dataset into data/raw/",
            RAW_DIR,
        )

    # One record per customer, shared by all their invoices. A customer is billed in one
    # currency under one contract, and their name is what a bank statement's payer field
    # is matched against, so it must not change from one invoice to the next.
    customers: dict[str, tuple[Customer, str]] = {}

    invoices: list[Invoice] = []
    for i in range(rows):
        customer_id = f"CUST-{2000 + (i % CUSTOMER_COUNT):04d}"
        if customer_id not in customers:
            customers[customer_id] = (
                Customer(
                    customer_id=customer_id,
                    name=fake.company(),
                    contact_name=fake.name(),
                    phone=fake.phone_number(),
                    email=fake.company_email(),
                    gstin=_fake_gstin(fake),
                ),
                rng.choice(CURRENCIES),
            )
        customer, currency = customers[customer_id]
        days_overdue = _sample_days_overdue(rng, lateness)
        due = as_of - timedelta(days=days_overdue)
        # Typical B2B net terms.
        terms = rng.choice([15, 30, 30, 45, 60])
        invoice_date = due - timedelta(days=terms)

        reminder_count = min(rng.randint(0, 1 + days_overdue // 21), 5)
        last_reminder = (
            as_of - timedelta(days=rng.randint(1, max(2, days_overdue)))
            if reminder_count
            else None
        )

        invoice = Invoice(
            invoice_id=f"INV-{100000 + i}",
            customer=customer,
            amount=_sample_amount(rng, amounts),
            currency=currency,
            invoice_date=invoice_date,
            due_date=due,
            days_overdue=days_overdue,
            hsn_sac_code=rng.choice(HSN_SAC_CODES),
            dispute_flag=rng.random() < DISPUTE_RATE,
            reminder_count=reminder_count,
            last_reminder_date=last_reminder,
            # Populated only once a real agent call extracts a commitment - this is the
            # field the judge/refinement loop cares most about.
            promise_to_pay_date=None,
        )
        invoices.append(invoice.model_copy(update={"risk_tier": classify_risk(invoice)}))

    return invoices


def write_csv(invoices: list[Invoice], out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=FIELDNAMES, lineterminator="\n")
        writer.writeheader()
        for inv in invoices:
            writer.writerow(
                {
                    "invoice_id": inv.invoice_id,
                    "customer_id": inv.customer.customer_id,
                    "customer_name": inv.customer.name,
                    "contact_name": inv.customer.contact_name,
                    "phone": inv.customer.phone,
                    "email": inv.customer.email,
                    "gstin": inv.customer.gstin,
                    "hsn_sac_code": inv.hsn_sac_code,
                    "amount": f"{inv.amount:.2f}",
                    "currency": inv.currency,
                    "invoice_date": inv.invoice_date.isoformat(),
                    "due_date": inv.due_date.isoformat(),
                    "days_overdue": inv.days_overdue,
                    "dispute_flag": inv.dispute_flag,
                    "reminder_count": inv.reminder_count,
                    "last_reminder_date": (
                        inv.last_reminder_date.isoformat() if inv.last_reminder_date else ""
                    ),
                    "promise_to_pay_date": "",
                    "risk_tier": inv.risk_tier.value if inv.risk_tier else "",
                }
            )


def load_invoices(path: Path | None = None) -> list[Invoice]:
    """Read the generated dataset back into Invoice models."""
    path = path or DEFAULT_OUTPUT
    if not path.exists():
        raise FileNotFoundError(
            f"{path} not found. Run: uv run python -m data.generate_dataset --rows 500 --seed 42"
        )
    invoices: list[Invoice] = []
    with path.open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            invoices.append(
                Invoice(
                    invoice_id=row["invoice_id"],
                    customer=Customer(
                        customer_id=row["customer_id"],
                        name=row["customer_name"],
                        contact_name=row["contact_name"] or None,
                        phone=row["phone"] or None,
                        email=row["email"] or None,
                        gstin=row["gstin"] or None,
                    ),
                    amount=Decimal(row["amount"]),
                    currency=row["currency"],
                    invoice_date=date.fromisoformat(row["invoice_date"]),
                    due_date=date.fromisoformat(row["due_date"]),
                    days_overdue=int(row["days_overdue"]),
                    hsn_sac_code=row["hsn_sac_code"] or None,
                    dispute_flag=row["dispute_flag"].lower() == "true",
                    reminder_count=int(row["reminder_count"]),
                    last_reminder_date=(
                        date.fromisoformat(row["last_reminder_date"])
                        if row["last_reminder_date"]
                        else None
                    ),
                    promise_to_pay_date=(
                        date.fromisoformat(row["promise_to_pay_date"])
                        if row["promise_to_pay_date"]
                        else None
                    ),
                    risk_tier=row["risk_tier"] or None,
                )
            )
    return invoices


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rows", type=int, default=500)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--base-csv", type=Path, default=None)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--as-of",
        type=date.fromisoformat,
        default=date(2026, 8, 1),
        help="Reference date for ageing. Fixed by default so output is reproducible.",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    base_csv = find_base_csv(args.base_csv)
    if base_csv:
        logger.info("Using Kaggle base: %s", base_csv)

    invoices = generate(args.rows, args.seed, base_csv, args.as_of)
    write_csv(invoices, args.out)

    tiers: dict[str, int] = {}
    for inv in invoices:
        key = inv.risk_tier.value if inv.risk_tier else "none"
        tiers[key] = tiers.get(key, 0) + 1
    logger.info("Wrote %d invoices to %s", len(invoices), args.out)
    logger.info("Risk tier distribution: %s", dict(sorted(tiers.items())))
    logger.info("Disputed: %d", sum(i.dispute_flag for i in invoices))
    return 0


if __name__ == "__main__":
    sys.exit(main())
