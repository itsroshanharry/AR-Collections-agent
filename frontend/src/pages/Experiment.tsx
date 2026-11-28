import { useQuery } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { Link, useParams } from "react-router-dom";

import { ApiError, api, unwrap, type Schemas } from "../api/client";
import {
  Badge,
  Bar,
  Card,
  Empty,
  ErrorNote,
  Loading,
  PageHeader,
  StateBadge,
  Table,
  Td,
  Th,
  cx,
} from "../components/ui";
import { dateTime, label, pct } from "../lib/format";

const ACTIVE = new Set(["pending", "running"]);

export function Experiment() {
  const { experimentId = "" } = useParams();
  const run = useQuery({
    queryKey: ["experiment", experimentId],
    queryFn: async () =>
      unwrap(await api.GET("/experiments/{experiment_id}", { params: { path: { experiment_id: experimentId } } })),
    refetchInterval: (q) => (q.state.data && ACTIVE.has(q.state.data.status) ? 3000 : false),
  });
  const active = !!run.data && ACTIVE.has(run.data.status);
  const results = useQuery({
    queryKey: ["experiment-results", experimentId],
    queryFn: async () =>
      unwrap(
        await api.GET("/experiments/{experiment_id}/results", {
          params: { path: { experiment_id: experimentId } },
        }),
      ),
    enabled: !!run.data,
    // A 409 means "no scenario finished yet": keep polling rather than erroring.
    retry: (n, e) => !(e instanceof ApiError && e.status === 409) && n < 1,
    refetchInterval: active ? 5000 : false,
  });

  if (run.isLoading) return <Loading what="Loading experiment" />;
  if (run.error || !run.data) return <ErrorNote error={run.error} />;
  const r = run.data;
  const noResultsYet = results.error instanceof ApiError && results.error.status === 409;

  return (
    <>
      <div className="mb-2 text-sm">
        <Link to="/experiments" className="text-accent hover:underline">
          ← Experiments
        </Link>
      </div>
      <PageHeader
        title={
          <span className="flex flex-wrap items-center gap-2">
            Run <span className="font-mono">{r.id.slice(0, 8)}</span> <StateBadge state={r.status} />
          </span>
        }
        subtitle={
          <>
            <span className="font-mono">A: {r.variant_a_id}</span> vs{" "}
            <span className="font-mono">B: {r.variant_b_id}</span> · started {dateTime(r.created_at)}
            {r.completed_at && <> · finished {dateTime(r.completed_at)}</>}
          </>
        }
      />

      {(active || r.status === "failed") && (
        <Card className="mb-6">
          <div className="mb-2 flex justify-between text-sm">
            <span>{active ? "Running scenarios…" : "Run failed"}</span>
            <span className="tabular text-muted">
              {r.completed_count} / {r.scenario_count || "?"}
            </span>
          </div>
          <Bar value={r.completed_count} max={r.scenario_count} tone={r.status === "failed" ? "bad" : "accent"} />
          {r.error && <p className="mt-2 text-sm text-bad">{r.error}</p>}
          {active && (
            <p className="mt-2 text-xs text-muted">
              Each scenario is saved as soon as it finishes, so the numbers below are provisional until the run
              completes. If the server restarts, the run picks up from the last saved scenario.
            </p>
          )}
        </Card>
      )}

      {results.isLoading ? (
        <Loading what="Loading results" />
      ) : noResultsYet ? (
        <Card>
          <Empty>No scenario has finished yet. Results appear here as each one completes.</Empty>
        </Card>
      ) : results.error || !results.data ? (
        <ErrorNote error={results.error} />
      ) : (
        <Results data={results.data} provisional={r.status !== "completed"} />
      )}
    </>
  );
}

function Results({ data, provisional }: { data: Schemas["ResultsOut"]; provisional: boolean }) {
  const { variant_a: a, variant_b: b, significance: sig } = data;
  const total = data.scenario_count;
  const adversarial = data.per_scenario.filter((s) => s.is_adversarial).length;

  return (
    <div className="flex flex-col gap-6">
      <Card>
        <div className="flex flex-wrap items-center gap-2">
          {provisional && <Badge tone="warn">Provisional</Badge>}
          <Badge tone={sig.significant ? "good" : "neutral"}>
            {sig.significant ? "Significant" : "Not significant"} · p = {sig.p_value.toFixed(3)}
          </Badge>
        </div>
        <p className="mt-3 text-lg font-semibold">{data.verdict}</p>
        <p className="mt-1 text-sm text-muted">{sig.interpretation}</p>

        <div className="mt-5">
          <HeadToHead a={a.wins} ties={data.ties} b={b.wins} />
        </div>
        <div className="mt-4 grid gap-3 sm:grid-cols-2">
          <WinRate name="A" summary={a} ci={sig.win_rate_ci_a} />
          <WinRate name="B" summary={b} ci={sig.win_rate_ci_b} />
        </div>
      </Card>

      <Card title="Side by side" flush>
        <Table>
          <thead>
            <tr>
              <Th>Measure</Th>
              <Th right>A</Th>
              <Th right>B</Th>
            </tr>
          </thead>
          <tbody>
            <Metric name="Mean judge score (1 to 5)" a={a.mean_composite} b={b.mean_composite} fmt={(v) => v.toFixed(2)} better="high" />
            <Metric name="Outcome read correctly" a={a.outcome_accuracy} b={b.outcome_accuracy} fmt={pct} better="high" />
            <Metric
              name="Promise date correct"
              a={a.promise_date_accuracy}
              b={b.promise_date_accuracy}
              fmt={pct}
              better="high"
            />
            <Metric name="Calls with a made-up figure" a={a.hallucination_rate} b={b.hallucination_rate} fmt={pct} better="low" />
            <Metric name="Calls with a compliance issue" a={a.compliance_failures} b={b.compliance_failures} fmt={String} better="low" />
            {adversarial > 0 && (
              <Metric
                name={`Manipulation attempts resisted (of ${adversarial})`}
                a={data.adversarial_held_a}
                b={data.adversarial_held_b}
                fmt={String}
                better="high"
              />
            )}
            <Metric name="Mean call time" a={a.mean_latency_ms} b={b.mean_latency_ms} fmt={seconds} better="low" />
            <Metric name="95th percentile call time" a={a.p95_latency_ms} b={b.p95_latency_ms} fmt={seconds} better="low" />
            <Metric name="Errors" a={a.errors} b={b.errors} fmt={String} better="low" />
          </tbody>
        </Table>
        <p className="border-t border-line px-4 py-2 text-xs text-muted">
          Judge score difference, A minus B: {signed(sig.composite_diff)} (95% interval {signed(sig.composite_diff_ci[0])} to{" "}
          {signed(sig.composite_diff_ci[1])}). An interval that includes zero means no real difference.
        </p>
      </Card>

      {/* minmax(0, 1fr): a grid track otherwise grows to its widest table, pushing the
          page sideways on a phone instead of letting the table scroll inside its card. */}
      <div className="grid grid-cols-[minmax(0,1fr)] gap-6 lg:grid-cols-[minmax(0,1fr)_minmax(0,1fr)]">
        <Card title="Judge scores by dimension">
          <Dimensions a={a.dimension_means} b={b.dimension_means} />
        </Card>
        <Card title="By kind of scenario" flush>
          <Table>
            <thead>
              <tr>
                <Th>Tag</Th>
                <Th right>Scenarios</Th>
                <Th right>A / tie / B</Th>
                <Th right>Outcome right, A vs B</Th>
              </tr>
            </thead>
            <tbody>
              {Object.entries(data.by_tag).map(([tag, t]) => (
                <tr key={tag}>
                  <Td>{label(tag)}</Td>
                  <Td right>{t.scenarios}</Td>
                  <Td right>
                    {t.a_wins} / {t.ties} / {t.b_wins}
                  </Td>
                  <Td right>
                    {pct(t.a_outcome_accuracy)} vs {pct(t.b_outcome_accuracy)}
                  </Td>
                </tr>
              ))}
            </tbody>
          </Table>
        </Card>
      </div>

      <Card title={`Scenarios (${total})`} flush>
        <Table>
          <thead>
            <tr>
              <Th>Scenario</Th>
              <Th>Expected</Th>
              <Th>A read it as</Th>
              <Th>B read it as</Th>
              <Th right>Score A / B</Th>
              <Th>Judge picked</Th>
            </tr>
          </thead>
          <tbody>
            {data.per_scenario.map((s) => (
              <tr key={s.scenario_id} className="hover:bg-surface-2">
                <Td>
                  <Link
                    to={`/experiments/${data.experiment_id}/scenarios/${encodeURIComponent(s.scenario_id)}`}
                    className="font-mono text-xs text-accent hover:underline"
                  >
                    {s.scenario_id}
                  </Link>
                  {s.is_adversarial && (
                    <span className="ml-2">
                      <Badge tone="warn">Manipulation</Badge>
                    </span>
                  )}
                </Td>
                <Td className="whitespace-nowrap">{label(s.expected_outcome)}</Td>
                <Td className="whitespace-nowrap">
                  <Read outcome={s.a_outcome} expected={s.expected_outcome} hallucinated={s.a_hallucinated} />
                </Td>
                <Td className="whitespace-nowrap">
                  <Read outcome={s.b_outcome} expected={s.expected_outcome} hallucinated={s.b_hallucinated} />
                </Td>
                <Td right>
                  {s.a_composite.toFixed(1)} / {s.b_composite.toFixed(1)}
                </Td>
                <Td>
                  <Winner winner={s.pairwise_winner} />
                </Td>
              </tr>
            ))}
          </tbody>
        </Table>
      </Card>
    </div>
  );
}

function HeadToHead({ a, ties, b }: { a: number; ties: number; b: number }) {
  const total = a + ties + b || 1;
  const seg = (n: number) => `${(n / total) * 100}%`;
  return (
    <div>
      <div className="mb-1.5 flex justify-between text-xs text-muted">
        <span>A won {a}</span>
        <span>{ties} tied</span>
        <span>B won {b}</span>
      </div>
      <div className="flex h-3 overflow-hidden rounded-full bg-surface-2" role="img" aria-label={`A ${a}, ties ${ties}, B ${b}`}>
        <div className="bg-accent" style={{ width: seg(a) }} />
        <div className="bg-muted/40" style={{ width: seg(ties) }} />
        <div className="bg-info" style={{ width: seg(b) }} />
      </div>
    </div>
  );
}

function WinRate({
  name,
  summary,
  ci,
}: {
  name: "A" | "B";
  summary: Schemas["VariantSummaryOut"];
  ci: [number, number] | number[];
}) {
  return (
    <div className="rounded-lg border border-line p-3">
      <div className="flex items-center gap-2 text-xs text-muted">
        <span className={cx("size-2 rounded-full", name === "A" ? "bg-accent" : "bg-info")} />
        Variant {name} · <span className="truncate font-mono">{summary.prompt_id}</span>
      </div>
      <div className="tabular mt-1 text-2xl font-semibold">{pct(summary.win_rate)}</div>
      <div className="text-xs text-muted">
        win rate, 95% interval {pct(ci[0])} to {pct(ci[1])}
      </div>
      {/* The interval drawn on a 0-100% track, with the point estimate marked. */}
      <div className="relative mt-2 h-2 rounded-full bg-surface-2">
        <div
          className={cx("absolute h-full rounded-full opacity-40", name === "A" ? "bg-accent" : "bg-info")}
          style={{ left: `${ci[0] * 100}%`, width: `${(ci[1] - ci[0]) * 100}%` }}
        />
        <div
          className={cx("absolute -top-0.5 h-3 w-1 rounded-full", name === "A" ? "bg-accent" : "bg-info")}
          style={{ left: `calc(${summary.win_rate * 100}% - 2px)` }}
        />
      </div>
    </div>
  );
}

function Metric({
  name,
  a,
  b,
  fmt,
  better,
}: {
  name: string;
  a: number | null | undefined;
  b: number | null | undefined;
  fmt: (v: number) => string;
  better: "high" | "low";
}) {
  const leader =
    a == null || b == null || a === b ? null : (better === "high" ? a > b : a < b) ? "A" : "B";
  const cell = (v: number | null | undefined, me: "A" | "B"): ReactNode => (
    <span className={cx(leader === me && "font-semibold text-good")}>{v == null ? "—" : fmt(v)}</span>
  );
  return (
    <tr>
      <Td>{name}</Td>
      <Td right>{cell(a, "A")}</Td>
      <Td right>{cell(b, "B")}</Td>
    </tr>
  );
}

const DIMENSIONS: Record<string, string> = {
  on_topic: "Stayed on topic",
  tone_appropriate: "Right tone for the risk tier",
  extraction_correct: "Captured the outcome",
  no_hallucination: "No made-up figures",
  compliant: "Compliant",
};

function Dimensions({ a, b }: { a: Record<string, number>; b: Record<string, number> }) {
  const keys = Array.from(new Set([...Object.keys(DIMENSIONS), ...Object.keys(a), ...Object.keys(b)])).filter(
    (k) => k in a || k in b,
  );
  if (!keys.length) return <Empty>No judge scores yet.</Empty>;
  return (
    <ul className="flex flex-col gap-3">
      {keys.map((k) => (
        <li key={k}>
          <div className="mb-1 text-sm">{DIMENSIONS[k] ?? label(k)}</div>
          <div className="grid grid-cols-[1.25rem_1fr_2.5rem] items-center gap-2 text-xs">
            <span className="text-muted">A</span>
            <Bar value={a[k] ?? 0} max={5} tone="accent" />
            <span className="tabular text-right">{a[k]?.toFixed(1) ?? "—"}</span>
            <span className="text-muted">B</span>
            <Bar value={b[k] ?? 0} max={5} tone="info" />
            <span className="tabular text-right">{b[k]?.toFixed(1) ?? "—"}</span>
          </div>
        </li>
      ))}
    </ul>
  );
}

function Read({
  outcome,
  expected,
  hallucinated,
}: {
  outcome: string | null | undefined;
  expected: string | null | undefined;
  hallucinated: boolean;
}) {
  const right = outcome != null && outcome === expected;
  return (
    <span className="inline-flex items-center gap-1.5">
      <span className={right ? "text-good" : "text-bad"} aria-label={right ? "correct" : "wrong"}>
        {right ? "✓" : "✗"}
      </span>
      {label(outcome)}
      {hallucinated && <Badge tone="bad">Made-up figure</Badge>}
    </span>
  );
}

export function Winner({ winner }: { winner: string | null | undefined }) {
  if (winner === "A") return <Badge tone="accent">A</Badge>;
  if (winner === "B") return <Badge tone="info">B</Badge>;
  if (winner === "tie") return <Badge>Tie</Badge>;
  return <span className="text-muted">—</span>;
}

function seconds(ms: number): string {
  return `${(ms / 1000).toFixed(1)} s`;
}

function signed(v: number): string {
  return `${v > 0 ? "+" : ""}${v.toFixed(2)}`;
}
