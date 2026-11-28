import { useQuery } from "@tanstack/react-query";
import { Link, useParams } from "react-router-dom";

import { api, unwrap, type Schemas } from "../api/client";
import { Transcript } from "../components/Transcript";
import { Badge, Card, ErrorNote, Loading, PageHeader, cx } from "../components/ui";
import { date, label } from "../lib/format";
import { Winner } from "./Experiment";

const DIMENSIONS: Record<string, string> = {
  on_topic: "On topic",
  tone_appropriate: "Tone",
  extraction_correct: "Outcome captured",
  no_hallucination: "No made-up figures",
  compliant: "Compliant",
};

export function Scenario() {
  const { experimentId = "", scenarioId = "" } = useParams();
  const { data, error, isLoading } = useQuery({
    queryKey: ["scenario", experimentId, scenarioId],
    queryFn: async () =>
      unwrap(
        await api.GET("/experiments/{experiment_id}/scenarios/{scenario_id}", {
          params: { path: { experiment_id: experimentId, scenario_id: scenarioId } },
        }),
      ),
  });

  if (isLoading) return <Loading what="Loading scenario" />;
  if (error || !data) return <ErrorNote error={error} />;

  // The judge sees the two transcripts in random order, so position cannot bias it.
  const order =
    data.presented_a_first == null ? null : data.presented_a_first ? "A's call first" : "B's call first";

  return (
    <>
      <div className="mb-2 text-sm">
        <Link to={`/experiments/${experimentId}`} className="text-accent hover:underline">
          ← Run {experimentId.slice(0, 8)}
        </Link>
      </div>
      <PageHeader
        title={<span className="font-mono text-lg break-all">{data.scenario_id}</span>}
        subtitle={data.description ?? "This scenario has since been removed from the eval set."}
      />

      <div className="mb-6 grid grid-cols-[minmax(0,1fr)] gap-6 lg:grid-cols-[minmax(0,1fr)_minmax(0,1fr)]">
        <Card title="The scripted customer">
          <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-2 text-sm">
            <dt className="text-muted">Persona</dt>
            <dd>{data.customer_persona ?? "—"}</dd>
            <dt className="text-muted">Right answer</dt>
            <dd>
              {label(data.expected_outcome)}
              {data.expected_promise_date && <> on {date(data.expected_promise_date)}</>}
            </dd>
            <dt className="text-muted">Tags</dt>
            <dd className="flex flex-wrap gap-1">
              {data.is_adversarial && <Badge tone="warn">Manipulation attempt</Badge>}
              {data.tags.map((t) => (
                <Badge key={t}>{label(t)}</Badge>
              ))}
            </dd>
          </dl>
        </Card>
        <Card title="Head-to-head judge">
          <div className="flex items-center gap-2 text-sm">
            Picked <Winner winner={data.pairwise_winner} />
            {order && <span className="text-xs text-muted">· shown {order}, without labels</span>}
          </div>
          <p className="mt-2 text-sm">{data.pairwise_reason || "No reason recorded."}</p>
        </Card>
      </div>

      <div className="grid grid-cols-[minmax(0,1fr)] gap-6 lg:grid-cols-[minmax(0,1fr)_minmax(0,1fr)]">
        <Variant run={data.variant_a} name="A" expected={data.expected_outcome} won={data.pairwise_winner === "A"} />
        <Variant run={data.variant_b} name="B" expected={data.expected_outcome} won={data.pairwise_winner === "B"} />
      </div>
    </>
  );
}

function Variant({
  run,
  name,
  expected,
  won,
}: {
  run: Schemas["VariantRunOut"] | null | undefined;
  name: "A" | "B";
  expected: string | null | undefined;
  won: boolean;
}) {
  if (!run) {
    return (
      <Card title={`Variant ${name}`}>
        <p className="text-sm text-muted">This variant has no result for this scenario.</p>
      </Card>
    );
  }
  const right = run.extracted_outcome != null && run.extracted_outcome === expected;
  return (
    <Card
      className={cx(won && "ring-2 ring-good/40")}
      title={
        <span className="flex flex-wrap items-center gap-2">
          Variant {name}
          <span className="font-mono text-xs font-normal text-muted">{run.prompt_id}</span>
        </span>
      }
      action={won ? <Badge tone="good">Judge&apos;s pick</Badge> : undefined}
    >
      {run.error ? (
        <div className="mb-3">
          <ErrorNote error={new Error(run.error)} />
        </div>
      ) : null}

      <div className="mb-4 flex flex-wrap gap-x-5 gap-y-2 text-sm">
        <div>
          <div className="text-xs text-muted">Outcome read as</div>
          <span className={right ? "text-good" : "text-bad"}>
            {right ? "✓" : "✗"} {label(run.extracted_outcome)}
          </span>
          {run.promise_date_match === false && <div className="text-xs text-bad">wrong promise date</div>}
        </div>
        <div>
          <div className="text-xs text-muted">Judge score</div>
          <span className="tabular font-semibold">{run.composite.toFixed(2)}</span>
          <span className="text-muted"> / 5</span>
        </div>
        <div>
          <div className="text-xs text-muted">Call time</div>
          <span className="tabular">{(run.total_latency_ms / 1000).toFixed(1)} s</span>
          <span className="text-xs text-muted"> · {(run.mean_turn_latency_ms / 1000).toFixed(1)} s per turn</span>
        </div>
        <div>
          <div className="text-xs text-muted">Model provider</div>
          <span>{run.provider || "—"}</span>
        </div>
      </div>

      {Object.keys(run.scores).length > 0 && (
        <div className="mb-4 flex flex-wrap gap-1.5">
          {Object.entries(run.scores).map(([k, v]) => (
            <Badge key={k} tone={v >= 4 ? "good" : v >= 3 ? "warn" : "bad"}>
              {DIMENSIONS[k] ?? label(k)} {v}
            </Badge>
          ))}
        </div>
      )}

      {run.hallucinated_figures.length > 0 && (
        <p className="mb-2 text-sm text-bad">
          Said figures not on the invoice: {run.hallucinated_figures.join(", ")}
        </p>
      )}
      {run.compliance_violations.length > 0 && (
        <ul className="mb-2 list-disc pl-5 text-sm text-bad">
          {run.compliance_violations.map((v, i) => (
            <li key={i}>{v}</li>
          ))}
        </ul>
      )}
      {run.judge_rationale && (
        <p className="mb-4 rounded-lg bg-surface-2 px-3 py-2 text-sm">
          <span className="text-xs font-medium text-muted">Judge: </span>
          {run.judge_rationale}
        </p>
      )}

      <Transcript text={run.transcript} flagged={run.hallucinated_figures} />
    </Card>
  );
}
