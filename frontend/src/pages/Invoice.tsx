import { useQuery } from "@tanstack/react-query";
import { useState, type FormEvent } from "react";
import { Link, useParams } from "react-router-dom";

import { api, unwrap, type Schemas } from "../api/client";
import { OutcomeForm } from "../components/OutcomeForm";
import {
  Badge,
  Button,
  Card,
  Empty,
  ErrorNote,
  Field,
  Loading,
  PageHeader,
  Stat,
  StateBadge,
  Table,
  Td,
  Th,
  TierBadge,
  inputClass,
} from "../components/ui";
import { date, dateTime, label, money, relative } from "../lib/format";
import { useAction } from "../lib/useAction";

export function Invoice() {
  const { invoiceId = "" } = useParams();
  const [panel, setPanel] = useState<"outcome" | "payment" | null>(null);
  const { data, error, isLoading } = useQuery({
    queryKey: ["invoice", invoiceId],
    queryFn: async () =>
      unwrap(await api.GET("/dunning/invoices/{invoice_id}", { params: { path: { invoice_id: invoiceId } } })),
  });
  const dnc = useAction(
    async (value: boolean) =>
      unwrap(
        await api.PATCH("/dunning/invoices/{invoice_id}", {
          params: { path: { invoice_id: invoiceId } },
          body: { do_not_call: value },
        }),
      ),
    { success: (r) => (r.do_not_call ? "Do not call: calls and SMS now go out as email." : "Calls allowed again.") },
  );

  if (isLoading) return <Loading />;
  if (error || !data) return <ErrorNote error={error} />;
  const s = data.state;
  const closed = s.state === "closed";

  return (
    <>
      <PageHeader
        title={
          <span className="flex flex-wrap items-center gap-2">
            {s.invoice_id} <StateBadge state={s.state} /> <TierBadge tier={s.tier} />
          </span>
        }
        subtitle={
          <>
            <Link className="underline" to={`/customers/${s.customer_id}`}>
              {s.customer_name}
            </Link>{" "}
            · {s.customer_id}
            {s.closed_reason && ` · closed: ${label(s.closed_reason)}`}
          </>
        }
        actions={
          !closed && (
            <>
              <Button busy={dnc.isPending} onClick={() => dnc.mutate(!s.do_not_call)}>
                {s.do_not_call ? "Allow calls" : "Do not call"}
              </Button>
              <Button onClick={() => setPanel(panel === "payment" ? null : "payment")}>Post payment</Button>
              <Button variant="primary" onClick={() => setPanel(panel === "outcome" ? null : "outcome")}>
                Record call outcome
              </Button>
            </>
          )
        }
      />

      {panel && (
        <Card className="mb-4" title={panel === "outcome" ? "Record a call outcome" : "Post a payment by hand"}>
          {panel === "outcome" ? (
            <OutcomeForm invoiceId={s.invoice_id} accountSize={data.other_open_invoices.length} onDone={() => setPanel(null)} />
          ) : (
            <PaymentForm state={s} onDone={() => setPanel(null)} />
          )}
        </Card>
      )}

      <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        <Stat label="Outstanding" value={money(s.amount_outstanding, s.currency)} />
        <Stat label="Days overdue" value={data.days_overdue_today} />
        <Stat
          label="Cadence"
          value={`${label(s.cadence_tier)} · step ${s.cadence_step + 1}`}
          hint={s.broken_promises ? `${s.broken_promises} broken promise(s)` : "No broken promises"}
        />
        <Stat
          label="Next action"
          value={s.next_action_channel ? label(s.next_action_channel) : "None"}
          hint={
            s.next_action_channel
              ? `${relative(s.next_action_at)}${s.do_not_call ? " · do not call" : ""}`
              : "Waiting on a person or a date"
          }
        />
      </div>

      <div className="mt-4 grid gap-4 lg:grid-cols-[1fr_22rem]">
        <Card title="Timeline" flush>
          <ol className="divide-y divide-line">
            {[...data.timeline].reverse().map((e, n) => (
              <TimelineItem key={`${e.created_at}-${n}`} event={e} />
            ))}
          </ol>
        </Card>

        <div className="flex flex-col gap-4">
          <Card title="Rest of the account" flush>
            {data.other_open_invoices.length === 0 ? (
              <Empty>No other invoices in active dunning.</Empty>
            ) : (
              <ul className="divide-y divide-line">
                {data.other_open_invoices.map((i) => (
                  <li key={i.invoice_id} className="flex items-center justify-between px-4 py-2 text-sm">
                    <Link className="hover:underline" to={`/invoices/${i.invoice_id}`}>
                      {i.invoice_id}
                    </Link>
                    <span className="tabular text-muted">
                      {money(i.amount_outstanding, i.currency)} · {i.days_overdue}d
                    </span>
                  </li>
                ))}
              </ul>
            )}
          </Card>
          <Card title="Tasks" flush>
            {data.tasks.length === 0 ? (
              <Empty>No follow-up tasks.</Empty>
            ) : (
              <ul className="divide-y divide-line">
                {data.tasks.map((t) => (
                  <li key={t.id} className="px-4 py-2.5 text-sm">
                    <div className="flex items-center justify-between gap-2">
                      <span className="font-medium">{t.title}</span>
                      <StateBadge state={t.status} />
                    </div>
                    <div className="text-xs text-muted">
                      {label(t.assignee)} · due {date(t.due_date)}
                      {t.resolution && ` · ${label(t.resolution)}`}
                    </div>
                  </li>
                ))}
              </ul>
            )}
          </Card>
        </div>
      </div>

      <Card title="Contact history" className="mt-4" flush>
        {data.contact_attempts.length === 0 ? (
          <Empty>Not contacted yet.</Empty>
        ) : (
          <Table>
            <thead>
              <tr>
                <Th>When</Th>
                <Th>Channel</Th>
                <Th>Outcome</Th>
              </tr>
            </thead>
            <tbody>
              {data.contact_attempts.map((a, n) => (
                <tr key={n}>
                  <Td>{dateTime(a.attempted_at)}</Td>
                  <Td>{label(a.channel)}</Td>
                  <Td>{label(a.outcome)}</Td>
                </tr>
              ))}
            </tbody>
          </Table>
        )}
      </Card>
    </>
  );
}

function TimelineItem({ event }: { event: Schemas["EventOut"] }) {
  const detail = Object.entries(event.detail ?? {}).filter(
    ([, v]) => v !== "" && v !== null && !(Array.isArray(v) && v.length === 0),
  );
  const changed = event.from_state && event.from_state !== event.to_state;
  return (
    <li className="px-4 py-3 text-sm">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <span className="font-medium">{label(event.event)}</span>
        <span className="text-xs text-muted">{dateTime(event.created_at)}</span>
      </div>
      {changed && (
        <div className="mt-1 flex items-center gap-1 text-xs">
          <StateBadge state={event.from_state} /> <span className="text-muted">→</span> <StateBadge state={event.to_state} />
        </div>
      )}
      {detail.length > 0 && (
        <dl className="mt-1.5 flex flex-wrap gap-x-4 gap-y-1 text-xs text-muted">
          {detail.map(([k, v]) => (
            <div key={k}>
              <dt className="inline">{label(k)}: </dt>
              <dd className="inline text-ink">
                {Array.isArray(v) ? v.map((x) => <Badge key={String(x)}>{value(x)}</Badge>) : value(v)}
              </dd>
            </div>
          ))}
        </dl>
      )}
    </li>
  );
}

/** Enum-like values ("promise_to_pay") read as labels; ids, dates and amounts as is. */
function value(v: unknown): string {
  const s = String(v);
  return /^[a-z_]+$/.test(s) ? label(s) : s;
}

function PaymentForm({ state, onDone }: { state: Schemas["InvoiceStateOut"]; onDone: () => void }) {
  const [amount, setAmount] = useState(state.amount_outstanding);
  const [reference, setReference] = useState("");
  const pay = useAction(
    async () =>
      unwrap(
        await api.POST("/dunning/invoices/{invoice_id}/payments", {
          params: { path: { invoice_id: state.invoice_id } },
          body: { amount, reference },
        }),
      ),
    {
      success: (r) =>
        r.state === "closed" ? `${r.invoice_id} is paid and closed.` : `Balance now ${money(r.amount_outstanding, r.currency)}.`,
      onDone,
    },
  );
  const submit = (e: FormEvent) => {
    e.preventDefault();
    pay.mutate(undefined);
  };
  return (
    <form onSubmit={submit} className="flex flex-wrap items-end gap-3">
      <Field label={`Amount (${state.currency})`}>
        <input required inputMode="decimal" className={inputClass} value={amount} onChange={(e) => setAmount(e.target.value)} />
      </Field>
      <Field label="Reference">
        <input className={inputClass} value={reference} onChange={(e) => setReference(e.target.value)} />
      </Field>
      <Button variant="primary" type="submit" busy={pay.isPending}>
        Post payment
      </Button>
      <p className="w-full text-xs text-muted">
        Bank credits normally arrive through cash application, which matches them to invoices. This posts one by hand.
      </p>
    </form>
  );
}
