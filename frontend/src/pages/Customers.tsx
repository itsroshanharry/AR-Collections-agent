import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Link } from "react-router-dom";

import { api, unwrap } from "../api/client";
import { Button, Card, Empty, ErrorNote, Loading, PageHeader, Table, Td, Th, TierBadge, inputClass } from "../components/ui";
import { inrCompact, totals } from "../lib/format";

const PAGE = 25;
type Sort = "outstanding" | "name" | "tier";

export function Customers() {
  const [q, setQ] = useState("");
  const [tier, setTier] = useState("");
  const [withTasks, setWithTasks] = useState(false);
  const [sort, setSort] = useState<Sort>("outstanding");
  const [offset, setOffset] = useState(0);

  const { data, error, isLoading, isFetching } = useQuery({
    queryKey: ["customers", q, tier, withTasks, sort, offset],
    queryFn: async () =>
      unwrap(
        await api.GET("/dunning/customers", {
          params: {
            query: {
              q: q || undefined,
              tier: tier || undefined,
              has_open_tasks: withTasks ? true : undefined,
              sort,
              limit: PAGE,
              offset,
            },
          },
        }),
      ),
    placeholderData: keepPreviousData,
  });

  const reset = () => setOffset(0);

  return (
    <>
      <PageHeader
        title="Customers"
        subtitle="Collections work the account: every customer with their balance, their riskiest open invoice and the work waiting on them."
      />
      <div className="mb-3 flex flex-wrap items-center gap-2">
        <input
          className={`${inputClass} w-64`}
          placeholder="Search name or customer id"
          value={q}
          onChange={(e) => {
            setQ(e.target.value);
            reset();
          }}
        />
        <select
          className={inputClass}
          value={tier}
          onChange={(e) => {
            setTier(e.target.value);
            reset();
          }}
        >
          <option value="">All tiers</option>
          {["low", "medium", "high", "critical"].map((t) => (
            <option key={t} value={t}>
              {t[0]!.toUpperCase() + t.slice(1)}
            </option>
          ))}
        </select>
        <select className={inputClass} value={sort} onChange={(e) => setSort(e.target.value as Sort)}>
          <option value="outstanding">Largest exposure</option>
          <option value="tier">Riskiest first</option>
          <option value="name">Name</option>
        </select>
        <label className="flex items-center gap-2 text-sm">
          <input
            type="checkbox"
            checked={withTasks}
            onChange={(e) => {
              setWithTasks(e.target.checked);
              reset();
            }}
          />
          With open tasks
        </label>
      </div>

      <Card flush>
        {isLoading ? (
          <Loading />
        ) : error || !data ? (
          <div className="p-4">
            <ErrorNote error={error} />
          </div>
        ) : !data.customers.length ? (
          <Empty>No customers match.</Empty>
        ) : (
          <>
            <Table>
              <thead>
                <tr>
                  <Th>Customer</Th>
                  <Th right>Invoices</Th>
                  <Th>Riskiest</Th>
                  <Th right>In dunning</Th>
                  <Th right>Outstanding</Th>
                  <Th right>Open tasks</Th>
                </tr>
              </thead>
              <tbody className={isFetching ? "opacity-60" : ""}>
                {data.customers.map((c) => (
                  <tr key={c.customer_id}>
                    <Td>
                      <Link className="font-medium hover:underline" to={`/customers/${c.customer_id}`}>
                        {c.customer_name}
                      </Link>
                      <div className="text-xs text-muted">{c.customer_id}</div>
                    </Td>
                    <Td right>
                      {c.open_invoices} <span className="text-muted">/ {c.invoices}</span>
                    </Td>
                    <Td>
                      <TierBadge tier={c.tier} />
                    </Td>
                    <Td right>{totals(c.overdue_in_dunning)}</Td>
                    <Td right>
                      {totals(c.outstanding)}
                      {Object.keys(c.outstanding).some((cur) => cur !== "INR") && (
                        <div className="text-xs text-muted">≈ {inrCompact(c.outstanding_inr)}</div>
                      )}
                    </Td>
                    <Td right>{c.open_tasks || <span className="text-muted">0</span>}</Td>
                  </tr>
                ))}
              </tbody>
            </Table>
            <div className="flex items-center justify-between px-4 py-3 text-sm text-muted">
              <span>
                {offset + 1}–{Math.min(offset + PAGE, data.total)} of {data.total}
              </span>
              <div className="flex gap-2">
                <Button disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - PAGE))}>
                  Previous
                </Button>
                <Button disabled={offset + PAGE >= data.total} onClick={() => setOffset(offset + PAGE)}>
                  Next
                </Button>
              </div>
            </div>
          </>
        )}
      </Card>
    </>
  );
}
