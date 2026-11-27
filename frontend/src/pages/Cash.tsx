import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useRef, useState, type FormEvent } from "react";
import { Link } from "react-router-dom";

import { api, unwrap, type Schemas } from "../api/client";
import { Allocations, TransactionsTable } from "../components/TransactionsTable";
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
  cx,
  inputClass,
} from "../components/ui";
import { label, money, pct } from "../lib/format";
import { useAction } from "../lib/useAction";

type Ingest = Schemas["IngestOut"];
type Txn = Schemas["TransactionOut"];

const STATUSES = ["", "matched", "unmatched", "applied", "dismissed"];

export function Cash() {
  const queryClient = useQueryClient();
  const fileInput = useRef<HTMLInputElement>(null);
  const [status, setStatus] = useState("");
  const { data: lastIngest } = useQuery<Ingest | null>({
    queryKey: ["lastIngest"],
    queryFn: () => null,
    staleTime: Infinity,
    initialData: null,
  });
  const exceptions = useQuery({
    queryKey: ["transactions", "unmatched"],
    queryFn: async () => unwrap(await api.GET("/cash/transactions", { params: { query: { status: "unmatched" } } })),
  });
  const all = useQuery({
    queryKey: ["transactions", "all", status],
    queryFn: async () =>
      unwrap(await api.GET("/cash/transactions", { params: { query: { status: status || undefined, limit: 50 } } })),
  });

  const keep = (r: Ingest) => queryClient.setQueryData(["lastIngest"], r);
  const summary = (r: Ingest) =>
    `${r.received} credits: ${r.matched} matched, ${r.unmatched} for a person` +
    (r.duplicates ? `, ${r.duplicates} already received` : "") +
    `. ${r.invoices_settled} invoice(s) settled.`;

  const simulate = useAction(
    async () => unwrap(await api.POST("/cash/simulate", { params: { query: { count: 8 } } })),
    { success: summary, onDone: keep },
  );
  const upload = useAction(
    async (text: string) =>
      unwrap(
        await api.POST("/cash/statement", {
          body: text as never,
          bodySerializer: (b: unknown) => String(b),
          headers: { "content-type": "text/csv" },
        }),
      ),
    { success: summary, onDone: keep },
  );

  const onFile = async (file: File | undefined) => {
    if (!file) return;
    upload.mutate(await file.text());
    if (fileInput.current) fileInput.current.value = "";
  };

  return (
    <>
      <PageHeader
        title="Cash application"
        subtitle="Bank credits matched to invoices. Clean payments close invoices with no person involved; anything the rules cannot place safely waits here with the reason."
        actions={
          <>
            <input
              ref={fileInput}
              type="file"
              accept=".csv,text/csv"
              className="hidden"
              onChange={(e) => onFile(e.target.files?.[0])}
            />
            <Button busy={upload.isPending} onClick={() => fileInput.current?.click()}>
              Upload statement CSV
            </Button>
            <Button variant="primary" busy={simulate.isPending} onClick={() => simulate.mutate(undefined)}>
              Simulate bank feed
            </Button>
          </>
        }
      />

      {lastIngest && <LatestBatch batch={lastIngest} />}

      <Card
        title={
          <span className="flex items-center gap-2">
            Exceptions queue {exceptions.data && <Badge tone={exceptions.data.length ? "warn" : "good"}>{exceptions.data.length}</Badge>}
          </span>
        }
        className="mt-4"
        flush
      >
        {exceptions.isLoading ? (
          <Loading />
        ) : exceptions.error ? (
          <div className="p-4">
            <ErrorNote error={exceptions.error} />
          </div>
        ) : !exceptions.data?.length ? (
          <Empty>No unmatched payments. Every credit received so far has been applied.</Empty>
        ) : (
          <ul className="divide-y divide-line">
            {exceptions.data.map((t) => (
              <ExceptionItem key={t.id} txn={t} />
            ))}
          </ul>
        )}
      </Card>

      <Card
        title="All bank credits"
        className="mt-4"
        flush
        action={
          <select className={inputClass} value={status} onChange={(e) => setStatus(e.target.value)}>
            {STATUSES.map((s) => (
              <option key={s} value={s}>
                {s ? label(s) : "Every status"}
              </option>
            ))}
          </select>
        }
      >
        {all.isLoading ? (
          <Loading />
        ) : all.data?.length ? (
          <TransactionsTable rows={all.data} />
        ) : (
          <Empty>No bank credits yet. Simulate a bank feed or upload a statement.</Empty>
        )}
      </Card>

      <p className="mt-4 text-xs text-muted">
        Statement CSV columns: <code>value_date, amount, currency, reference, payer_name, utr</code>. The UTR is unique, so
        uploading the same statement twice applies nothing twice.
      </p>
    </>
  );
}

function LatestBatch({ batch }: { batch: Ingest }) {
  return (
    <Card title="Latest batch" flush>
      <div className="grid grid-cols-2 gap-3 p-4 lg:grid-cols-4">
        <Stat label="Credits received" value={batch.received} hint={batch.duplicates ? `${batch.duplicates} already seen` : undefined} />
        <Stat label="Matched automatically" value={batch.matched} tone="good" />
        <Stat label="Left for a person" value={batch.unmatched} tone={batch.unmatched ? "warn" : "default"} />
        <Stat
          label="Auto-match rate"
          value={pct(batch.auto_match_rate)}
          hint={`${batch.invoices_settled} settled · ${batch.invoices_part_paid} part paid`}
        />
      </div>
      <Table>
        <thead>
          <tr>
            <Th>UTR</Th>
            <Th right>Amount</Th>
            <Th>Result</Th>
            <Th>Why</Th>
            <Th>Applied to</Th>
          </tr>
        </thead>
        <tbody>
          {batch.lines.map((l) => (
            <tr key={l.utr}>
              <Td className="text-xs text-muted">{l.utr}</Td>
              <Td right>{money(l.amount, l.currency)}</Td>
              <Td>
                <div className="flex flex-col items-start gap-1">
                  <StateBadge state={l.status} />
                  {l.rule !== "none" && <Badge>{label(l.rule)}</Badge>}
                </div>
              </Td>
              <Td className="max-w-sm text-xs text-muted">{l.reason}</Td>
              <Td>
                <Allocations items={l.allocations} currency={l.currency} />
              </Td>
            </tr>
          ))}
        </tbody>
      </Table>
    </Card>
  );
}

function ExceptionItem({ txn }: { txn: Txn }) {
  const [mode, setMode] = useState<"apply" | "dismiss" | null>(null);
  return (
    <li className="px-4 py-3">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0 text-sm">
          <div className="flex flex-wrap items-center gap-2">
            <span className="tabular font-medium">{money(txn.amount, txn.currency)}</span>
            <span className="text-muted">from {txn.payer_name || "an unnamed payer"}</span>
            <span className="text-xs text-muted">{txn.utr}</span>
          </div>
          <div className="text-xs text-muted">Narrative: {txn.reference || "none"}</div>
          <div className="mt-1 text-xs text-warn">{txn.reason}</div>
          {txn.customer_id && (
            <div className="mt-0.5 text-xs text-muted">
              Likely customer:{" "}
              <Link className="text-accent hover:underline" to={`/customers/${txn.customer_id}`}>
                {txn.customer_id}
              </Link>
            </div>
          )}
        </div>
        <div className="flex gap-2">
          <Button onClick={() => setMode(mode === "apply" ? null : "apply")}>Apply</Button>
          <Button variant="danger" onClick={() => setMode(mode === "dismiss" ? null : "dismiss")}>
            Dismiss
          </Button>
        </div>
      </div>
      {mode === "apply" && <ApplyForm txn={txn} onDone={() => setMode(null)} />}
      {mode === "dismiss" && <DismissForm txn={txn} onDone={() => setMode(null)} />}
    </li>
  );
}

function ApplyForm({ txn, onDone }: { txn: Txn; onDone: () => void }) {
  const account = useQuery({
    queryKey: ["customer", txn.customer_id],
    enabled: Boolean(txn.customer_id),
    queryFn: async () =>
      unwrap(
        await api.GET("/dunning/customers/{customer_id}", { params: { path: { customer_id: txn.customer_id! } } }),
      ),
  });
  const candidates = (account.data?.invoices ?? []).filter((i) => i.state !== "closed" && i.currency === txn.currency);
  const [invoiceId, setInvoiceId] = useState("");
  const [amount, setAmount] = useState(txn.amount);
  const [note, setNote] = useState("");
  const apply = useAction(
    async () =>
      unwrap(
        await api.POST("/cash/transactions/{txn_id}/apply", {
          params: { path: { txn_id: txn.id } },
          body: { allocations: [{ invoice_id: invoiceId.trim(), amount }], note },
        }),
      ),
    { success: () => `Applied ${money(amount, txn.currency)} to ${invoiceId}.`, onDone },
  );
  const submit = (e: FormEvent) => {
    e.preventDefault();
    apply.mutate(undefined);
  };
  return (
    <form onSubmit={submit} className="mt-3 flex flex-wrap items-end gap-3 rounded-lg bg-surface-2 p-3">
      <Field label="Invoice">
        {candidates.length ? (
          <select required className={inputClass} value={invoiceId} onChange={(e) => {
            setInvoiceId(e.target.value);
            const inv = candidates.find((c) => c.invoice_id === e.target.value);
            if (inv && Number(inv.amount_outstanding) < Number(txn.amount)) setAmount(inv.amount_outstanding);
          }}>
            <option value="">Choose…</option>
            {candidates.map((c) => (
              <option key={c.invoice_id} value={c.invoice_id}>
                {c.invoice_id} · {money(c.amount_outstanding, c.currency)}
              </option>
            ))}
          </select>
        ) : (
          <input required placeholder="INV-100123" className={inputClass} value={invoiceId} onChange={(e) => setInvoiceId(e.target.value)} />
        )}
      </Field>
      <Field label={`Amount (${txn.currency})`}>
        <input required inputMode="decimal" className={cx(inputClass, "w-32")} value={amount} onChange={(e) => setAmount(e.target.value)} />
      </Field>
      <Field label="Note">
        <input className={inputClass} placeholder="e.g. refund the excess" value={note} onChange={(e) => setNote(e.target.value)} />
      </Field>
      <Button variant="primary" type="submit" busy={apply.isPending}>
        Apply payment
      </Button>
      <p className="w-full text-xs text-muted">Anything not allocated stays recorded on the credit, for a refund.</p>
    </form>
  );
}

function DismissForm({ txn, onDone }: { txn: Txn; onDone: () => void }) {
  const [note, setNote] = useState("");
  const dismiss = useAction(
    async () =>
      unwrap(
        await api.POST("/cash/transactions/{txn_id}/dismiss", { params: { path: { txn_id: txn.id } }, body: { note } }),
      ),
    { success: () => `Dismissed ${txn.utr}.`, onDone },
  );
  const submit = (e: FormEvent) => {
    e.preventDefault();
    dismiss.mutate(undefined);
  };
  return (
    <form onSubmit={submit} className="mt-3 flex flex-wrap items-end gap-3 rounded-lg bg-surface-2 p-3">
      <Field label="Why is this not a customer payment?">
        <input
          required
          className={cx(inputClass, "w-80")}
          placeholder="Transfer from our own savings account"
          value={note}
          onChange={(e) => setNote(e.target.value)}
        />
      </Field>
      <Button variant="danger" type="submit" busy={dismiss.isPending}>
        Dismiss
      </Button>
    </form>
  );
}
