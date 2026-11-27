import { useQuery } from "@tanstack/react-query";
import { Link, useParams } from "react-router-dom";

import { api, unwrap } from "../api/client";
import { TransactionsTable } from "../components/TransactionsTable";
import {
  Card,
  Empty,
  ErrorNote,
  Loading,
  PageHeader,
  Stat,
  StateBadge,
  Table,
  Td,
  Th,
  TierBadge,
} from "../components/ui";
import { date, label, money, relative, totals } from "../lib/format";

export function Customer() {
  const { customerId = "" } = useParams();
  const account = useQuery({
    queryKey: ["customer", customerId],
    queryFn: async () =>
      unwrap(await api.GET("/dunning/customers/{customer_id}", { params: { path: { customer_id: customerId } } })),
  });
  const credits = useQuery({
    queryKey: ["transactions", "customer", customerId],
    queryFn: async () =>
      unwrap(await api.GET("/cash/transactions", { params: { query: { customer_id: customerId, limit: 50 } } })),
  });

  if (account.isLoading) return <Loading />;
  if (account.error || !account.data) return <ErrorNote error={account.error} />;
  const a = account.data;
  const open = a.invoices.filter((i) => i.state !== "closed").length;

  return (
    <>
      <PageHeader
        title={a.customer_name}
        subtitle={
          <>
            {a.customer_id} · <Link className="underline" to="/customers">All customers</Link>
          </>
        }
      />
      <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        <Stat label="In active dunning" value={totals(a.overdue_in_dunning)} hint="What a call asks for" />
        <Stat label="Outstanding" value={totals(a.outstanding)} hint="Every invoice not yet closed" />
        <Stat label="Open invoices" value={`${open} / ${a.invoices.length}`} />
        <Stat label="Open tasks" value={a.open_tasks.length} tone={a.open_tasks.length ? "warn" : "default"} />
      </div>

      <Card title="Invoices" className="mt-4" flush>
        <Table>
          <thead>
            <tr>
              <Th>Invoice</Th>
              <Th>State</Th>
              <Th>Tier</Th>
              <Th right>Outstanding</Th>
              <Th right>Days overdue</Th>
              <Th>Next action</Th>
              <Th>Promised for</Th>
            </tr>
          </thead>
          <tbody>
            {a.invoices.map((i) => (
              <tr key={i.invoice_id}>
                <Td>
                  <Link className="font-medium hover:underline" to={`/invoices/${i.invoice_id}`}>
                    {i.invoice_id}
                  </Link>
                </Td>
                <Td>
                  <StateBadge state={i.state} />
                  {i.closed_reason && <span className="ml-1 text-xs text-muted">{label(i.closed_reason)}</span>}
                </Td>
                <Td>
                  <TierBadge tier={i.tier} />
                </Td>
                <Td right>{money(i.amount_outstanding, i.currency)}</Td>
                <Td right>{i.days_overdue_today}</Td>
                <Td>
                  {i.next_action_channel ? (
                    <>
                      {label(i.next_action_channel)}{" "}
                      <span className="text-xs text-muted">{relative(i.next_action_at)}</span>
                    </>
                  ) : (
                    <span className="text-muted">Waiting on a person or date</span>
                  )}
                </Td>
                <Td>{date(i.promise_to_pay_date)}</Td>
              </tr>
            ))}
          </tbody>
        </Table>
      </Card>

      <div className="mt-4 grid gap-4 lg:grid-cols-2">
        <Card title="Open tasks" flush>
          {a.open_tasks.length === 0 ? (
            <Empty>Nothing is waiting on a person for this customer.</Empty>
          ) : (
            <ul className="divide-y divide-line">
              {a.open_tasks.map((t) => (
                <li key={t.id} className="px-4 py-3 text-sm">
                  <div className="font-medium">{t.title}</div>
                  <div className="text-xs text-muted">
                    {label(t.assignee)} · {t.invoice_id} · due {date(t.due_date)}
                  </div>
                </li>
              ))}
            </ul>
          )}
        </Card>
        <Card title="Bank credits" flush>
          {credits.isLoading ? (
            <Loading />
          ) : credits.data?.length ? (
            <TransactionsTable rows={credits.data} compact />
          ) : (
            <Empty>No payments received from this customer yet.</Empty>
          )}
        </Card>
      </div>
    </>
  );
}
