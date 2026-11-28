import { useQuery } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { Link, useNavigate } from "react-router-dom";

import { api, unwrap, type Schemas } from "../api/client";
import {
  Badge,
  Bar,
  Button,
  Card,
  Empty,
  ErrorNote,
  Field,
  Loading,
  PageHeader,
  StateBadge,
  Table,
  Td,
  Th,
  inputClass,
} from "../components/ui";
import { dateTime, label, relative } from "../lib/format";
import { useAction } from "../lib/useAction";

/** Groq's free tier (8,000 tokens a minute) paces a scenario (two simulated calls, two
 *  judgings, one pairwise judgment) to roughly this long, measured on a real run. */
const SECONDS_PER_SCENARIO = 120;
const ACTIVE = new Set(["pending", "running"]);

export function Experiments() {
  const prompts = useQuery({
    queryKey: ["prompts"],
    queryFn: async () => unwrap(await api.GET("/prompts")),
  });
  const runs = useQuery({
    queryKey: ["experiments"],
    queryFn: async () => unwrap(await api.GET("/experiments")),
    // Poll only while something is running.
    refetchInterval: (q) => (q.state.data?.some((r) => ACTIVE.has(r.status)) ? 3000 : false),
  });

  const sync = useAction(async () => unwrap(await api.POST("/prompts/sync")), {
    success: (r) =>
      `Imported ${r.imported.length} new and refreshed ${r.updated.length} prompt version(s) from the agent.`,
  });

  return (
    <>
      <PageHeader
        title="Prompt experiments"
        subtitle="Run two versions of the agent's prompt against the same fixed set of scripted customers. A blind judge picks the better call in each scenario, and a sign test says whether the difference is real or noise."
      />

      <div className="grid grid-cols-[minmax(0,1fr)] gap-6 lg:grid-cols-[minmax(0,1fr)_minmax(0,22rem)]">
        <Card
          title="Prompt versions"
          flush
          action={
            <Button busy={sync.isPending} onClick={() => sync.mutate(undefined)}>
              Sync from agent
            </Button>
          }
        >
          {prompts.isLoading ? (
            <Loading />
          ) : prompts.error ? (
            <div className="p-4">
              <ErrorNote error={prompts.error} />
            </div>
          ) : !prompts.data?.length ? (
            <Empty>
              No prompt versions yet. <strong>Sync from agent</strong> imports the YAML prompts the
              voice agent runs, so an experiment tests exactly what goes to customers.
            </Empty>
          ) : (
            <PromptList prompts={prompts.data} />
          )}
        </Card>

        <NewRun prompts={prompts.data ?? []} />
      </div>

      <Card title="Runs" flush className="mt-6">
        {runs.isLoading ? (
          <Loading />
        ) : runs.error ? (
          <div className="p-4">
            <ErrorNote error={runs.error} />
          </div>
        ) : !runs.data?.length ? (
          <Empty>No experiments yet.</Empty>
        ) : (
          <Table>
            <thead>
              <tr>
                <Th>Run</Th>
                <Th>A vs B</Th>
                <Th>Status</Th>
                <Th>Progress</Th>
                <Th>Started</Th>
              </tr>
            </thead>
            <tbody>
              {runs.data.map((run) => (
                <RunRow key={run.id} run={run} />
              ))}
            </tbody>
          </Table>
        )}
      </Card>
    </>
  );
}

function PromptList({ prompts }: { prompts: Schemas["PromptOut"][] }) {
  const [open, setOpen] = useState<string | null>(null);
  return (
    <ul className="divide-y divide-line">
      {prompts.map((p) => (
        <li key={p.id} className="px-4 py-3">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <div className="min-w-0">
              <div className="font-mono text-sm break-all">{p.id}</div>
              <div className="mt-1 flex flex-wrap items-center gap-1.5 text-xs text-muted">
                <span>v{p.version}</span>
                <span aria-hidden>·</span>
                <Badge tone="accent">{label(p.tone)}</Badge>
                {p.applies_to_tier.length > 0 && (
                  <>
                    <span aria-hidden>·</span>
                    <span>used for {p.applies_to_tier.join(", ")} risk</span>
                  </>
                )}
              </div>
            </div>
            <Button variant="ghost" onClick={() => setOpen(open === p.id ? null : p.id)}>
              {open === p.id ? "Hide prompt" : "View prompt"}
            </Button>
          </div>
          {p.changelog && <p className="mt-2 text-sm text-muted">{p.changelog}</p>}
          {open === p.id && (
            <pre className="mt-3 max-h-80 overflow-auto rounded-lg bg-surface-2 p-3 text-xs leading-relaxed whitespace-pre-wrap">
              {p.template}
            </pre>
          )}
        </li>
      ))}
    </ul>
  );
}

function NewRun({ prompts }: { prompts: Schemas["PromptOut"][] }) {
  const navigate = useNavigate();
  const [a, setA] = useState("");
  const [b, setB] = useState("");
  const [limit, setLimit] = useState("3");

  // Default to the first two versions once they load.
  useEffect(() => {
    const [first, second] = prompts;
    if (first && second && !a && !b) {
      setA(first.id);
      setB(second.id);
    }
  }, [prompts, a, b]);

  const start = useAction(
    async () =>
      unwrap(
        await api.POST("/experiments", {
          body: {
            variant_a_id: a,
            variant_b_id: b,
            eval_set: "eval_scenarios.yaml",
            seed: 42,
            limit: limit ? Number(limit) : null,
          },
        }),
      ),
    {
      success: () => "Experiment started. Results fill in as each scenario finishes.",
      onDone: (run) => navigate(`/experiments/${run.id}`),
    },
  );

  const scenarios = limit ? Number(limit) : 28;
  const minutes = Math.max(1, Math.round((scenarios * SECONDS_PER_SCENARIO) / 60));
  const same = a !== "" && a === b;

  return (
    <Card title="Start a run">
      <form
        className="flex flex-col gap-3"
        onSubmit={(e) => {
          e.preventDefault();
          start.mutate(undefined);
        }}
      >
        <Field label="Variant A">
          <select className={inputClass} value={a} onChange={(e) => setA(e.target.value)} required>
            <option value="" disabled>
              Choose a prompt
            </option>
            {prompts.map((p) => (
              <option key={p.id} value={p.id}>
                {p.id}
              </option>
            ))}
          </select>
        </Field>
        <Field label="Variant B">
          <select className={inputClass} value={b} onChange={(e) => setB(e.target.value)} required>
            <option value="" disabled>
              Choose a prompt
            </option>
            {prompts.map((p) => (
              <option key={p.id} value={p.id}>
                {p.id}
              </option>
            ))}
          </select>
        </Field>
        <Field label="Scenarios">
          <select className={inputClass} value={limit} onChange={(e) => setLimit(e.target.value)}>
            <option value="3">First 3 (quick check)</option>
            <option value="10">First 10</option>
            <option value="">All 28</option>
          </select>
        </Field>
        {same && <p className="text-sm text-bad">Pick two different prompts.</p>}
        <p className="text-xs text-muted">
          About {minutes} minutes on Groq&apos;s free tier, which is paced to 8,000 tokens a minute. You
          can leave this page; the run carries on in the background and survives a server restart.
        </p>
        <Button variant="primary" type="submit" busy={start.isPending} disabled={!a || !b || same}>
          Start experiment
        </Button>
      </form>
    </Card>
  );
}

function RunRow({ run }: { run: Schemas["ExperimentOut"] }) {
  const total = run.scenario_count || 0;
  return (
    <tr className="hover:bg-surface-2">
      <Td>
        <Link to={`/experiments/${run.id}`} className="font-mono text-accent hover:underline">
          {run.id.slice(0, 8)}
        </Link>
      </Td>
      <Td>
        <div className="font-mono text-xs">
          <div>A: {run.variant_a_id}</div>
          <div>B: {run.variant_b_id}</div>
        </div>
      </Td>
      <Td>
        <StateBadge state={run.status} />
        {run.error && <div className="mt-1 max-w-xs text-xs text-bad">{run.error}</div>}
      </Td>
      <Td className="min-w-40">
        <div className="tabular mb-1 text-xs text-muted">
          {run.completed_count} / {total || "?"} scenarios
        </div>
        <Bar value={run.completed_count} max={total} tone={run.status === "failed" ? "bad" : "accent"} />
      </Td>
      <Td className="whitespace-nowrap">
        <div>{dateTime(run.created_at)}</div>
        <div className="text-xs text-muted">{relative(run.created_at)}</div>
      </Td>
    </tr>
  );
}
