import { useState, type FormEvent } from "react";

import { api, unwrap, type Schemas } from "../api/client";
import { label } from "../lib/format";
import { useAction } from "../lib/useAction";
import { Button, Field, inputClass } from "./ui";

type Outcome = Schemas["OutcomeType"];

const OUTCOMES: Outcome[] = [
  "promise_to_pay",
  "partial_payment",
  "dispute_raised",
  "already_paid",
  "refused",
  "no_answer",
  "unclear",
];

/** Record a call outcome by hand: what a collector does after a call the voice agent did
 *  not make. The workflow takes it from there (next state, follow-up tasks). */
export function OutcomeForm({
  invoiceId,
  accountSize,
  onDone,
}: {
  invoiceId: string;
  accountSize: number;
  onDone?: () => void;
}) {
  const [outcome, setOutcome] = useState<Outcome>("promise_to_pay");
  const [promiseDate, setPromiseDate] = useState("");
  const [amount, setAmount] = useState("");
  const [reason, setReason] = useState("");
  const [reference, setReference] = useState("");
  const [coversAccount, setCoversAccount] = useState(accountSize > 0);

  const record = useAction(
    async () =>
      unwrap(
        await api.POST("/dunning/invoices/{invoice_id}/outcomes", {
          params: { path: { invoice_id: invoiceId } },
          body: {
            outcome,
            promise_to_pay_date: promiseDate || null,
            promised_amount: amount || null,
            dispute_reason: reason || null,
            payment_reference: reference || null,
            covers_account: coversAccount,
            note: "",
          },
        }),
      ),
    {
      success: (r) => {
        const covered = r.account_invoices.length
          ? ` Also covered ${r.account_invoices.length} other invoice(s)${r.promise_covers_account ? ", all on the promise" : ""}.`
          : "";
        const verified = r.payment_verified_by ? " The payment was found in the bank feed and applied." : "";
        return `${invoiceId} is now ${label(r.state).toLowerCase()}; ${r.tasks_opened.length} task(s) opened.${covered}${verified}`;
      },
      onDone: () => onDone?.(),
    },
  );

  const needsDate = outcome === "promise_to_pay" || outcome === "partial_payment";
  const submit = (e: FormEvent) => {
    e.preventDefault();
    record.mutate(undefined);
  };

  return (
    <form onSubmit={submit} className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
      <Field label="Outcome">
        <select className={inputClass} value={outcome} onChange={(e) => setOutcome(e.target.value as Outcome)}>
          {OUTCOMES.map((o) => (
            <option key={o} value={o}>
              {label(o)}
            </option>
          ))}
        </select>
      </Field>
      {needsDate && (
        <>
          <Field label="Promised date">
            <input type="date" required className={inputClass} value={promiseDate} onChange={(e) => setPromiseDate(e.target.value)} />
          </Field>
          <Field label="Promised amount (blank = everything)">
            <input inputMode="decimal" className={inputClass} value={amount} onChange={(e) => setAmount(e.target.value)} />
          </Field>
        </>
      )}
      {outcome === "dispute_raised" && (
        <Field label="Reason">
          <input required className={inputClass} value={reason} onChange={(e) => setReason(e.target.value)} />
        </Field>
      )}
      {outcome === "already_paid" && (
        <Field label="Payment reference (UTR)">
          <input className={inputClass} value={reference} onChange={(e) => setReference(e.target.value)} />
        </Field>
      )}
      {accountSize > 0 && (
        <label className="flex items-center gap-2 text-sm sm:col-span-2 lg:col-span-3">
          <input type="checkbox" checked={coversAccount} onChange={(e) => setCoversAccount(e.target.checked)} />
          The call covered the whole account ({accountSize} other invoice{accountSize > 1 ? "s" : ""})
        </label>
      )}
      <div className="sm:col-span-2 lg:col-span-3">
        <Button variant="primary" type="submit" busy={record.isPending}>
          Record outcome
        </Button>
      </div>
    </form>
  );
}
