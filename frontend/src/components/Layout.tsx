import { useQuery } from "@tanstack/react-query";
import { NavLink, Outlet } from "react-router-dom";

import { api } from "../api/client";
import { cx } from "./ui";

const NAV = [
  { to: "/", label: "Overview", end: true },
  { to: "/calls", label: "Call queue" },
  { to: "/customers", label: "Customers" },
  { to: "/tasks", label: "Work queues" },
  { to: "/cash", label: "Cash application" },
  { to: "/experiments", label: "Prompt experiments" },
];

export function Layout() {
  return (
    <div className="min-h-screen md:flex">
      <aside className="border-b border-line bg-surface md:sticky md:top-0 md:h-screen md:w-56 md:shrink-0 md:border-r md:border-b-0">
        <div className="flex items-center justify-between gap-3 px-4 py-3 md:block md:py-5">
          <div className="flex items-center gap-2">
            <span className="grid size-7 place-items-center rounded-md bg-accent text-xs font-bold text-accent-ink">
              AR
            </span>
            <div className="leading-tight">
              <div className="text-sm font-semibold">Collections</div>
              <div className="text-xs text-muted">Operator console</div>
            </div>
          </div>
          <div className="md:mt-4">
            <HealthBadge />
          </div>
        </div>
        <nav className="flex gap-1 overflow-x-auto px-2 pb-2 md:flex-col md:px-3">
          {NAV.map((item) => (
            <NavLink
              key={item.to}
              to={item.to}
              end={item.end}
              className={({ isActive }) =>
                cx(
                  "rounded-lg px-3 py-1.5 text-sm whitespace-nowrap",
                  isActive ? "bg-accent-soft font-medium text-accent" : "text-muted hover:bg-surface-2 hover:text-ink",
                )
              }
            >
              {item.label}
            </NavLink>
          ))}
          <a
            href="/docs"
            className="rounded-lg px-3 py-1.5 text-sm whitespace-nowrap text-muted hover:bg-surface-2 hover:text-ink md:mt-4"
          >
            API docs ↗
          </a>
        </nav>
      </aside>
      <main className="min-w-0 flex-1 px-4 py-6 md:px-8">
        <div className="mx-auto max-w-6xl">
          <Outlet />
        </div>
      </main>
    </div>
  );
}

/** Polls readiness, so a free-tier cold start reads as "starting" rather than broken. */
function HealthBadge() {
  const { data, isError } = useQuery({
    queryKey: ["ready"],
    queryFn: async () => {
      const res = await api.GET("/health/ready");
      return { ok: res.response.ok };
    },
    refetchInterval: 30_000,
    retry: false,
  });
  const state = isError ? "down" : data === undefined ? "checking" : data.ok ? "ready" : "degraded";
  const styles = {
    ready: ["bg-good", "System ready"],
    degraded: ["bg-warn", "Starting up"],
    down: ["bg-bad", "Unreachable"],
    checking: ["bg-muted", "Checking"],
  }[state] as [string, string];
  return (
    <span className="inline-flex items-center gap-1.5 text-xs text-muted" title="GET /health/ready">
      <span className={cx("size-2 rounded-full", styles[0])} />
      {styles[1]}
    </span>
  );
}
