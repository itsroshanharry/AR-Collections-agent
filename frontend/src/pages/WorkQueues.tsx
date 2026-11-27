import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Link, useSearchParams } from "react-router-dom";

import { api, unwrap, type Schemas } from "../api/client";
import { Badge, Button, Card, Empty, ErrorNote, Loading, PageHeader, StateBadge, cx, inputClass } from "../components/ui";
import { date, label } from "../lib/format";
import { useAction } from "../lib/useAction";

const TEAMS = [
  { id: "", name: "All teams" },
  { id: "ar-ops", name: "AR operations" },
  { id: "disputes", name: "Disputes" },
  { id: "collections-lead", name: "Collections lead" },
  { id: "qa", name: "Call QA" },
];

/** What a resolution does to the invoice, so the person clicking knows. */
const EFFECT: Record<string, string> = {
  received: "closes it as paid",
  not_received: "counts as a broken promise",
  upheld: "closes it; nothing is owed",
  rejected: "resumes dunning",
  confirmed: "confirms it",
  not_found: "resumes dunning",
  resume_dunning: "restarts the cadence",
  write_off: "closes it as written off",
  legal: "closes it, sent to legal",
  corrected: "marks the capture as wrong",
};

export function WorkQueues() {
  const [params, setParams] = useSearchParams();
  const team = params.get("team") ?? "";
  const [showDone, setShowDone] = useState(false);

  const { data, error, isLoading } = useQuery({
    queryKey: ["tasks", team, showDone],
    queryFn: async () =>
      unwrap(
        await api.GET("/tasks", {
          params: { query: { assignee: team || undefined, status: showDone ? "" : "open", limit: 200 } },
        }),
      ),
  });

  return (
    <>
      <PageHeader
        title="Work queues"
        subtitle="Follow-up work the workflow opened for people, soonest due first. Each resolution moves the invoice on."
      />
      <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
        <div className="flex flex-wrap gap-1 rounded-lg border border-line bg-surface p-1">
          {TEAMS.map((t) => (
            <button
              key={t.id}
              onClick={() => setParams(t.id ? { team: t.id } : {})}
              className={cx(
                "rounded-md px-3 py-1 text-sm",
                team === t.id ? "bg-accent-soft font-medium text-accent" : "text-muted hover:text-ink",
              )}
            >
              {t.name}
            </button>
          ))}
        </div>
        <label className="flex items-center gap-2 text-sm">
          <input type="checkbox" checked={showDone} onChange={(e) => setShowDone(e.target.checked)} />
          Include resolved
        </label>
      </div>

      <Card flush>
        {isLoading ? (
          <Loading />
        ) : error ? (
          <div className="p-4">
            <ErrorNote error={error} />
          </div>
        ) : !data?.length ? (
          <Empty>Nothing in this queue.</Empty>
        ) : (
          <ul className="divide-y divide-line">
            {data.map((t) => (
              <TaskItem key={t.id} task={t} />
            ))}
          </ul>
        )}
      </Card>
    </>
  );
}

function TaskItem({ task }: { task: Schemas["TaskOut"] }) {
  const [note, setNote] = useState("");
  const overdue = task.status === "open" && new Date(`${task.due_date}T23:59:59`) < new Date();
  const resolve = useAction(
    async (resolution: string) =>
      unwrap(await api.PATCH("/tasks/{task_id}", { params: { path: { task_id: task.id } }, body: { resolution, note } })),
    { success: (r) => `${r.title}: ${label(r.resolution)}.` },
  );
  const payload = Object.entries(task.payload ?? {}).filter(([, v]) => v !== "" && v !== null);

  return (
    <li className="px-4 py-3">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2 text-sm font-medium">
            {task.title}
            <Badge>{label(task.task_type)}</Badge>
            {overdue && <Badge tone="bad">Past SLA</Badge>}
          </div>
          <div className="mt-0.5 text-xs text-muted">
            <Link className="text-accent hover:underline" to={`/invoices/${task.invoice_id}`}>
              {task.invoice_id}
            </Link>{" "}
            · {label(task.assignee)} · due {date(task.due_date)}
          </div>
          {payload.length > 0 && (
            <div className="mt-1 flex flex-wrap gap-x-3 text-xs text-muted">
              {payload.map(([k, v]) => (
                <span key={k}>
                  {label(k)}: <span className="text-ink">{typeof v === "object" ? JSON.stringify(v) : String(v)}</span>
                </span>
              ))}
            </div>
          )}
        </div>
        {task.status === "open" ? (
          <div className="flex flex-wrap items-center gap-2">
            <input
              className={`${inputClass} w-44`}
              placeholder="Note (optional)"
              value={note}
              onChange={(e) => setNote(e.target.value)}
            />
            {task.allowed_resolutions.map((r) => (
              <Button
                key={r}
                title={EFFECT[r] ? `${label(r)}: ${EFFECT[r]}` : label(r)}
                busy={resolve.isPending && resolve.variables === r}
                disabled={resolve.isPending}
                onClick={() => resolve.mutate(r)}
              >
                {label(r)}
              </Button>
            ))}
          </div>
        ) : (
          <div className="text-right text-xs text-muted">
            <StateBadge state={task.status} /> {label(task.resolution)}
            {task.resolution_note && <div className="mt-1">“{task.resolution_note}”</div>}
          </div>
        )}
      </div>
    </li>
  );
}
