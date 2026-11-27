const LOCALE: Record<string, string> = { INR: "en-IN" };

/** "₹1,70,000.00" for INR (lakh grouping), "$1,250.00" for USD, and so on. */
export function money(amount: string | number, currency = "INR"): string {
  const value = typeof amount === "string" ? Number(amount) : amount;
  if (!Number.isFinite(value)) return String(amount);
  try {
    return new Intl.NumberFormat(LOCALE[currency] ?? "en-US", {
      style: "currency",
      currency,
      maximumFractionDigits: 2,
    }).format(value);
  } catch {
    return `${value.toFixed(2)} ${currency}`;
  }
}

/** Compact INR for headline figures: "₹12.4L", "₹1.2Cr". */
export function inrCompact(amount: string | number): string {
  const v = typeof amount === "string" ? Number(amount) : amount;
  if (v >= 1e7) return `₹${(v / 1e7).toFixed(1)}Cr`;
  if (v >= 1e5) return `₹${(v / 1e5).toFixed(1)}L`;
  if (v >= 1e3) return `₹${(v / 1e3).toFixed(1)}K`;
  return `₹${v.toFixed(0)}`;
}

/** Per-currency totals, never summed across currencies. */
export function totals(byCurrency: Record<string, string>): string {
  const parts = Object.entries(byCurrency).map(([cur, amt]) => money(amt, cur));
  return parts.length ? parts.join(" + ") : "—";
}

export function date(value: string | null | undefined): string {
  if (!value) return "—";
  const d = new Date(value.length === 10 ? `${value}T00:00:00` : value);
  return d.toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "numeric" });
}

export function dateTime(value: string | null | undefined): string {
  if (!value) return "—";
  return new Date(value).toLocaleString("en-GB", {
    day: "numeric",
    month: "short",
    hour: "2-digit",
    minute: "2-digit",
  });
}

export function relative(value: string | null | undefined): string {
  if (!value) return "—";
  const diff = new Date(value).getTime() - Date.now();
  const mins = Math.round(diff / 60000);
  const abs = Math.abs(mins);
  const fmt = new Intl.RelativeTimeFormat("en", { numeric: "auto" });
  if (abs < 60) return fmt.format(mins, "minute");
  if (abs < 60 * 36) return fmt.format(Math.round(mins / 60), "hour");
  return fmt.format(Math.round(mins / 1440), "day");
}

/** snake_case API values to sentence case labels: "paid_unverified" -> "Paid, unverified". */
const LABELS: Record<string, string> = {
  in_dunning: "In dunning",
  paid_unverified: "Paid, unverified",
  promise_to_pay: "Promise to pay",
  dispute_raised: "Dispute raised",
  already_paid: "Already paid",
  partial_payment: "Partial payment",
  no_answer: "No answer",
  verify_payment: "Verify payment",
  review_dispute: "Review dispute",
  reconcile_payment: "Reconcile payment",
  escalation_review: "Escalation review",
  review_call: "Review call",
  "ar-ops": "AR operations",
  "collections-lead": "Collections lead",
  disputes: "Disputes",
  qa: "Call QA",
  invoice_reference: "Invoice reference",
  invoice_references: "Several invoices",
  customer_amount: "Customer + amount",
  customer_account: "Whole account",
  customer_claim: "Customer's claim",
  none: "No match",
  manual: "Applied by hand",
};

export function label(value: string | null | undefined): string {
  if (!value) return "—";
  if (LABELS[value]) return LABELS[value];
  const words = value.replace(/[_:-]+/g, " ").trim();
  return words.charAt(0).toUpperCase() + words.slice(1);
}

export function pct(value: number | null | undefined): string {
  return value === null || value === undefined ? "—" : `${Math.round(value * 100)}%`;
}
