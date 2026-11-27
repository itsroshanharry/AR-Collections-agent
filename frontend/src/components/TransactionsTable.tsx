import type { ReactNode } from "react";
import { Link } from "react-router-dom";

import type { Schemas } from "../api/client";
import { dateTime, label, money } from "../lib/format";
import { Badge, StateBadge, Table, Td, Th } from "./ui";

type Txn = Schemas["TransactionOut"];
type Allocation = { invoice_id?: string; amount?: string; settles?: boolean };

export function Allocations({ items, currency }: { items: Allocation[]; currency: string }) {
  if (!items.length) return <span className="text-muted">—</span>;
  return (
    <div className="flex flex-col gap-0.5">
      {items.map((a) => (
        <span key={a.invoice_id} className="text-xs">
          <Link className="hover:underline" to={`/invoices/${a.invoice_id}`}>
            {a.invoice_id}
          </Link>{" "}
          <span className="tabular text-muted">{money(a.amount ?? "0", currency)}</span>
          {a.settles ? <span className="ml-1 text-good">settled</span> : <span className="ml-1 text-muted">part</span>}
        </span>
      ))}
    </div>
  );
}

export function TransactionsTable({
  rows,
  compact,
  actions,
}: {
  rows: Txn[];
  compact?: boolean;
  actions?: (txn: Txn) => ReactNode;
}) {
  return (
    <Table>
      <thead>
        <tr>
          <Th>Received</Th>
          <Th right>Amount</Th>
          {!compact && <Th>Payer and narrative</Th>}
          <Th>Status</Th>
          {!compact && <Th>Why</Th>}
          <Th>Applied to</Th>
          {actions && <Th />}
        </tr>
      </thead>
      <tbody>
        {rows.map((t) => (
          <tr key={t.id}>
            <Td>
              <div className="whitespace-nowrap">{dateTime(t.received_at)}</div>
              <div className="text-xs text-muted">{t.utr}</div>
            </Td>
            <Td right>{money(t.amount, t.currency)}</Td>
            {!compact && (
              <Td>
                <div>{t.payer_name || <span className="text-muted">No payer name</span>}</div>
                <div className="text-xs text-muted">{t.reference || "No narrative"}</div>
              </Td>
            )}
            <Td>
              <div className="flex flex-col items-start gap-1">
                <StateBadge state={t.status} />
                {t.match_rule !== "none" && <Badge>{label(t.match_rule)}</Badge>}
              </div>
            </Td>
            {!compact && (
              <Td className="max-w-xs text-xs text-muted">
                {t.reason}
                {t.resolution_note && <div className="mt-1 text-ink">Note: {t.resolution_note}</div>}
                {t.status === "unmatched" && t.customer_id && (
                  <div className="mt-1">
                    Likely customer:{" "}
                    <Link className="text-accent hover:underline" to={`/customers/${t.customer_id}`}>
                      {t.customer_id}
                    </Link>
                  </div>
                )}
              </Td>
            )}
            <Td>
              <Allocations items={(t.allocations ?? []) as Allocation[]} currency={t.currency} />
            </Td>
            {actions && <Td right>{actions(t)}</Td>}
          </tr>
        ))}
      </tbody>
    </Table>
  );
}
