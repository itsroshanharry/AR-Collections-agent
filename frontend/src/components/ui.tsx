import type { ButtonHTMLAttributes, ReactNode } from "react";

import { ApiError } from "../api/client";
import { label } from "../lib/format";

export function cx(...parts: (string | false | null | undefined)[]): string {
  return parts.filter(Boolean).join(" ");
}

export function PageHeader({
  title,
  subtitle,
  actions,
}: {
  title: ReactNode;
  subtitle?: ReactNode;
  actions?: ReactNode;
}) {
  return (
    <div className="mb-6 flex flex-wrap items-end justify-between gap-4">
      <div className="min-w-0">
        <h1 className="text-xl font-semibold tracking-tight">{title}</h1>
        {subtitle && <p className="mt-1 max-w-3xl text-sm text-muted">{subtitle}</p>}
      </div>
      {actions && <div className="flex flex-wrap items-center gap-2">{actions}</div>}
    </div>
  );
}

export function Card({
  title,
  action,
  children,
  className,
  flush,
}: {
  title?: ReactNode;
  action?: ReactNode;
  children: ReactNode;
  className?: string;
  flush?: boolean;
}) {
  return (
    <section className={cx("rounded-xl border border-line bg-surface", className)}>
      {(title || action) && (
        <header className="flex items-center justify-between gap-3 border-b border-line px-4 py-3">
          <h2 className="text-sm font-semibold">{title}</h2>
          {action}
        </header>
      )}
      <div className={flush ? "" : "p-4"}>{children}</div>
    </section>
  );
}

export function Stat({
  label: name,
  value,
  hint,
  tone = "default",
}: {
  label: string;
  value: ReactNode;
  hint?: ReactNode;
  tone?: "default" | "good" | "warn" | "bad";
}) {
  const color = { default: "text-ink", good: "text-good", warn: "text-warn", bad: "text-bad" }[tone];
  return (
    <div className="rounded-xl border border-line bg-surface p-4">
      <div className="text-xs font-medium text-muted">{name}</div>
      <div className={cx("tabular mt-1 text-2xl font-semibold", color)}>{value}</div>
      {hint && <div className="mt-1 text-xs text-muted">{hint}</div>}
    </div>
  );
}

type Tone = "neutral" | "accent" | "good" | "warn" | "bad" | "info";

const TONES: Record<Tone, string> = {
  neutral: "bg-surface-2 text-muted",
  accent: "bg-accent-soft text-accent",
  good: "bg-good-soft text-good",
  warn: "bg-warn-soft text-warn",
  bad: "bg-bad-soft text-bad",
  info: "bg-info-soft text-info",
};

export function Badge({ tone = "neutral", children }: { tone?: Tone; children: ReactNode }) {
  return (
    <span
      className={cx(
        "inline-flex items-center whitespace-nowrap rounded-md px-1.5 py-0.5 text-xs font-medium",
        TONES[tone],
      )}
    >
      {children}
    </span>
  );
}

const TIER_TONE: Record<string, Tone> = {
  low: "neutral",
  medium: "info",
  high: "warn",
  critical: "bad",
};

export function TierBadge({ tier }: { tier: string | null | undefined }) {
  if (!tier) return <span className="text-muted">—</span>;
  return <Badge tone={TIER_TONE[tier] ?? "neutral"}>{label(tier)}</Badge>;
}

const STATE_TONE: Record<string, Tone> = {
  new: "neutral",
  in_dunning: "accent",
  promised: "info",
  disputed: "warn",
  paid_unverified: "info",
  escalated: "bad",
  closed: "good",
  matched: "good",
  applied: "good",
  unmatched: "warn",
  dismissed: "neutral",
  duplicate: "neutral",
  open: "accent",
  done: "good",
  pending: "neutral",
  running: "accent",
  completed: "good",
  failed: "bad",
};

export function StateBadge({ state }: { state: string }) {
  return <Badge tone={STATE_TONE[state] ?? "neutral"}>{label(state)}</Badge>;
}

export function Button({
  variant = "secondary",
  busy,
  children,
  className,
  disabled,
  ...rest
}: ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: "primary" | "secondary" | "ghost" | "danger";
  busy?: boolean;
}) {
  const styles = {
    primary: "bg-accent text-accent-ink hover:opacity-90",
    secondary: "border border-line bg-surface hover:bg-surface-2",
    ghost: "hover:bg-surface-2",
    danger: "border border-line bg-surface text-bad hover:bg-bad-soft",
  }[variant];
  return (
    <button
      {...rest}
      disabled={disabled || busy}
      className={cx(
        "inline-flex items-center justify-center gap-2 rounded-lg px-3 py-1.5 text-sm font-medium",
        "transition disabled:cursor-not-allowed disabled:opacity-50",
        "focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent",
        styles,
        className,
      )}
    >
      {busy && <Spinner />}
      {children}
    </button>
  );
}

export function Spinner() {
  return (
    <span
      aria-hidden
      className="inline-block size-3.5 animate-spin rounded-full border-2 border-current border-r-transparent"
    />
  );
}

export function Loading({ what = "Loading" }: { what?: string }) {
  return (
    <div className="flex items-center gap-2 p-6 text-sm text-muted">
      <Spinner /> {what}…
    </div>
  );
}

export function ErrorNote({ error }: { error: unknown }) {
  const message =
    error instanceof ApiError
      ? error.message
      : error instanceof Error
        ? error.message
        : "Something went wrong.";
  return (
    <div role="alert" className="rounded-lg border border-line bg-bad-soft px-3 py-2 text-sm text-bad">
      {message}
    </div>
  );
}

export function Empty({ children }: { children: ReactNode }) {
  return <div className="p-6 text-center text-sm text-muted">{children}</div>;
}

/** A table that scrolls sideways inside its card on narrow screens instead of
 *  stretching the page. */
export function Table({ children }: { children: ReactNode }) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-left text-sm">{children}</table>
    </div>
  );
}

export function Th({ children, right }: { children?: ReactNode; right?: boolean }) {
  return (
    <th
      className={cx(
        "border-b border-line bg-surface-2 px-4 py-2 text-xs font-medium whitespace-nowrap text-muted",
        right && "text-right",
      )}
    >
      {children}
    </th>
  );
}

export function Td({
  children,
  right,
  className,
}: {
  children?: ReactNode;
  right?: boolean;
  className?: string;
}) {
  return (
    <td className={cx("border-b border-line px-4 py-2.5 align-top", right && "tabular text-right", className)}>
      {children}
    </td>
  );
}

/** A horizontal bar for proportions, so the overview needs no chart library. */
export function Bar({ value, max, tone = "accent" }: { value: number; max: number; tone?: Tone }) {
  const width = max > 0 ? Math.max(2, (value / max) * 100) : 0;
  const fill = {
    neutral: "bg-muted",
    accent: "bg-accent",
    good: "bg-good",
    warn: "bg-warn",
    bad: "bg-bad",
    info: "bg-info",
  }[tone];
  return (
    <div className="h-2 w-full overflow-hidden rounded-full bg-surface-2">
      <div className={cx("h-full rounded-full", fill)} style={{ width: `${width}%` }} />
    </div>
  );
}

export function Field({ label: name, children }: { label: string; children: ReactNode }) {
  return (
    <label className="flex flex-col gap-1 text-sm">
      <span className="text-xs font-medium text-muted">{name}</span>
      {children}
    </label>
  );
}

export const inputClass =
  "rounded-lg border border-line bg-surface px-2.5 py-1.5 text-sm outline-none focus:border-accent";

export { TIER_TONE };
