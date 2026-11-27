import { useQuery } from "@tanstack/react-query";
import { Fragment, useState } from "react";
import { Link } from "react-router-dom";

import { api, unwrap } from "../api/client";
import { OutcomeForm } from "../components/OutcomeForm";
import { Badge, Button, Card, Empty, ErrorNote, Loading, PageHeader, Table, Td, Th, TierBadge } from "../components/ui";
import { money, totals } from "../lib/format";
import { useAction } from "../lib/useAction";

export function CallQueue() {
  const [open, setOpen] = useState<string | null>(null);
  const { data, error, isLoading } = useQuery({
    queryKey: ["queue"],
    queryFn: async () => unwrap(await api.GET("/dunning/queue", { params: { query: { limit: 50 } } })),
    refetchInterval: 60_000,
  });
  const tick = useAction(async () => unwrap(await api.POST("/dunning/tick")), {
    success: (r) => `Scheduler pass: ${r.calls_due} calls due, ${r.emails_sent} emails sent.`,
  });

  return (
    <>
      <PageHeader
        title="Call queue"
        subtitle="One call per customer, about their most urgent invoice and covering the rest of the account. Only calls the cadence wants and the contact rules allow right now are listed."
        actions={
          <Button busy={tick.isPending} onClick={() => tick.mutate(undefined)}>
            Run scheduler pass
          </Button>
        }
      />
      <Card flush>
        {isLoading ? (
          <Loading />
        ) : error ? (
          <div className="p-4">
            <ErrorNote error={error} />
          </div>
        ) : !data?.length ? (
          <Empty>
            No calls are due right now. Calls go out 09:00 to 19:00 in the customer's own timezone, Monday to
            Saturday, within the weekly contact caps, so the queue is often empty outside those hours.
          </Empty>
        ) : (
          <Table>
            <thead>
              <tr>
                <Th>Customer</Th>
                <Th>Calling about</Th>
                <Th>Also on the account</Th>
                <Th right>Account total</Th>
                <Th>Tier</Th>
                <Th right>Priority</Th>
                <Th />
              </tr>
            </thead>
            <tbody>
              {data.map((call) => (
                <Fragment key={call.invoice_id}>
                  <tr>
                    <Td>
                      <Link className="font-medium hover:underline" to={`/customers/${call.customer_id}`}>
                        {call.customer_name}
                      </Link>
                      <div className="text-xs text-muted">{call.customer_id}</div>
                    </Td>
                    <Td>
                      <Link className="hover:underline" to={`/invoices/${call.invoice_id}`}>
                        {call.invoice_id}
                      </Link>
                      <div className="tabular text-xs text-muted">
                        {money(call.amount_outstanding, call.currency)} · {call.days_overdue} days overdue
                        {call.broken_promises > 0 && ` · ${call.broken_promises} broken promise(s)`}
                      </div>
                    </Td>
                    <Td>
                      {call.other_open_invoices.length ? (
                        <div className="flex flex-wrap gap-1">
                          {call.other_open_invoices.map((i) => (
                            <Link key={i.invoice_id} to={`/invoices/${i.invoice_id}`}>
                              <Badge>{i.invoice_id}</Badge>
                            </Link>
                          ))}
                        </div>
                      ) : (
                        <span className="text-muted">—</span>
                      )}
                    </Td>
                    <Td right>{totals(call.account_total)}</Td>
                    <Td>
                      <TierBadge tier={call.tier} />
                    </Td>
                    <Td right>{call.priority.toFixed(0)}</Td>
                    <Td right>
                      <Button variant="ghost" onClick={() => setOpen(open === call.invoice_id ? null : call.invoice_id)}>
                        {open === call.invoice_id ? "Close" : "Record outcome"}
                      </Button>
                    </Td>
                  </tr>
                  {open === call.invoice_id && (
                    <tr>
                      <td colSpan={7} className="border-b border-line bg-surface-2 px-4 py-4">
                        <OutcomeForm
                          invoiceId={call.invoice_id}
                          accountSize={call.other_open_invoices.length}
                          onDone={() => setOpen(null)}
                        />
                      </td>
                    </tr>
                  )}
                </Fragment>
              ))}
            </tbody>
          </Table>
        )}
      </Card>
    </>
  );
}
