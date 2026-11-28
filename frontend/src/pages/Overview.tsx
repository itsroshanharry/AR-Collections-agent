import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useNavigate } from "react-router-dom";

import { api, unwrap } from "../api/client";
import { Bar, Button, Card, Empty, ErrorNote, Loading, PageHeader, Stat, TIER_TONE } from "../components/ui";
import { inrCompact, label, pct } from "../lib/format";
import { useAction } from "../lib/useAction";

const TEAMS = ["ar-ops", "disputes", "collections-lead", "qa"];
const STATE_ORDER = ["new", "in_dunning", "promised", "paid_unverified", "disputed", "escalated", "closed"];

export function Overview() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const { data, error, isLoading } = useQuery({
    queryKey: ["summary"],
    queryFn: async () => unwrap(await api.GET("/dunning/summary")),
    refetchInterval: 30_000,
  });

  const sync = useAction(async () => unwrap(await api.POST("/dunning/sync", {})), {
    success: (r) => `Loaded ${r.created} invoices into the workflow (${r.disputed_on_arrival} already disputed).`,
  });
  const tick = useAction(async () => unwrap(await api.POST("/dunning/tick")), {
    success: (r) =>
      `Scheduler pass: ${r.emails_sent} emails, ${r.sms_sent} SMS, ${r.calls_due} calls due, ` +
      `${r.escalated} escalated, ${r.promises_broken} promises broken.`,
  });
  const feed = useAction(
    async () => unwrap(await api.POST("/cash/simulate", { params: { query: { count: 8 } } })),
    {
      success: (r) =>
        `${r.received} bank credits: ${r.matched} matched automatically, ${r.unmatched} need a person.`,
      onDone: (r) => {
        queryClient.setQueryData(["lastIngest"], r);
        navigate("/cash");
      },
    },
  );

  const reset = useAction(async () => unwrap(await api.POST("/admin/demo/reset", {})), {
    success: (r) => `Demo reset: ${r.reloaded?.created ?? 0} invoices reloaded, all activity cleared.`,
  });
  const confirmReset = () => {
    if (window.confirm("Wipe every outcome, payment, task and bank transaction, and reload the ledger?")) {
      reset.mutate(undefined);
    }
  };

  if (isLoading) return <Loading what="Loading the book" />;
  if (error || !data) return <ErrorNote error={error} />;

  const exposure = Object.values(data.outstanding_inr_by_tier).reduce((a, v) => a + Number(v), 0);
  const openTasks = Object.values(data.open_tasks_by_team).reduce((a, v) => a + v, 0);
  const overdueTasks = Object.values(data.overdue_tasks_by_team).reduce((a, v) => a + v, 0);
  const unmatched = data.bank_transactions_by_status.unmatched ?? 0;
  const maxTier = Math.max(...Object.values(data.outstanding_inr_by_tier).map(Number), 0);
  const maxState = Math.max(...Object.values(data.invoices_by_state), 0);

  if (data.invoices === 0) {
    return (
      <>
        <PageHeader title="Overview" />
        <Card>
          <Empty>
            <p className="mb-4">The workflow is empty. Load the demo ledger of overdue invoices to begin.</p>
            <Button need="operator" variant="primary" busy={sync.isPending} onClick={() => sync.mutate(undefined)}>
              Load ledger
            </Button>
          </Empty>
        </Card>
      </>
    );
  }

  return (
    <>
      <PageHeader
        title="Overview"
        subtitle="The book of overdue invoices, what the dunning workflow is doing with it, and the work left for people."
        actions={
          <>
            <Button need="admin" variant="ghost" busy={reset.isPending} onClick={confirmReset}>
              Reset demo
            </Button>
            <Button need="operator" busy={tick.isPending} onClick={() => tick.mutate(undefined)}>
              Run scheduler pass
            </Button>
            <Button need="operator" variant="primary" busy={feed.isPending} onClick={() => feed.mutate(undefined)}>
              Simulate bank feed
            </Button>
          </>
        }
      />

      <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        <Stat label="Open exposure" value={inrCompact(exposure)} hint="INR-normalised, all open invoices" />
        <Stat
          label="Customers to call now"
          value={data.customers_with_calls_due}
          hint={<Link className="underline" to="/calls">Open the call queue</Link>}
        />
        <Stat
          label="Open tasks"
          value={openTasks}
          tone={overdueTasks ? "warn" : "default"}
          hint={overdueTasks ? `${overdueTasks} past their SLA` : "None past SLA"}
        />
        <Stat
          label="Auto-match rate"
          value={pct(data.auto_match_rate)}
          tone={data.auto_match_rate === null ? "default" : data.auto_match_rate >= 0.8 ? "good" : "warn"}
          hint={unmatched ? `${unmatched} payments waiting for a person` : "Bank credits applied with no person"}
        />
      </div>

      <div className="mt-4 grid gap-4 lg:grid-cols-2">
        <Card title="Exposure by risk tier">
          <ul className="space-y-3">
            {Object.entries(data.outstanding_inr_by_tier).map(([tier, amount]) => (
              <li key={tier} className="grid grid-cols-[6rem_1fr_5rem] items-center gap-3 text-sm">
                <span>
                  {label(tier)} <span className="text-muted">· {data.invoices_by_tier[tier] ?? 0}</span>
                </span>
                <Bar value={Number(amount)} max={maxTier} tone={TIER_TONE[tier] ?? "accent"} />
                <span className="tabular text-right">{inrCompact(amount)}</span>
              </li>
            ))}
          </ul>
        </Card>

        <Card title="Invoices by state">
          <ul className="space-y-3">
            {STATE_ORDER.filter((s) => data.invoices_by_state[s]).map((state) => (
              <li key={state} className="grid grid-cols-[8rem_1fr_3rem] items-center gap-3 text-sm">
                <span>{label(state)}</span>
                <Bar value={data.invoices_by_state[state] ?? 0} max={maxState} />
                <span className="tabular text-right">{data.invoices_by_state[state]}</span>
              </li>
            ))}
          </ul>
        </Card>

        <Card title="Work queues" action={<Link className="text-sm text-accent" to="/tasks">View all</Link>}>
          <ul className="divide-y divide-line">
            {TEAMS.map((team) => {
              const open = data.open_tasks_by_team[team] ?? 0;
              const late = data.overdue_tasks_by_team[team] ?? 0;
              return (
                <li key={team} className="flex items-center justify-between py-2 text-sm">
                  <Link to={`/tasks?team=${team}`} className="hover:underline">
                    {label(team)}
                  </Link>
                  <span className="tabular">
                    {open} open
                    {late > 0 && <span className="ml-2 text-warn">{late} late</span>}
                  </span>
                </li>
              );
            })}
          </ul>
        </Card>

        <Card title="Cash application" action={<Link className="text-sm text-accent" to="/cash">Open</Link>}>
          {Object.keys(data.bank_transactions_by_status).length === 0 ? (
            <Empty>No bank credits yet. Simulate a bank feed to see invoices close on their own.</Empty>
          ) : (
            <ul className="divide-y divide-line">
              {Object.entries(data.bank_transactions_by_status).map(([status, count]) => (
                <li key={status} className="flex items-center justify-between py-2 text-sm">
                  <span>{label(status)}</span>
                  <span className="tabular">{count}</span>
                </li>
              ))}
            </ul>
          )}
        </Card>
      </div>
    </>
  );
}
