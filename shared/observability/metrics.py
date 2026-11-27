"""Prometheus metrics: one registry, every metric the system exposes, defined in one place.

Defined centrally rather than next to the code that records them so the full set is
reviewable at a glance (it is the contract a dashboard and its alerts are built on),
and so label names stay consistent across modules.

Label cardinality is kept bounded on purpose: provider, model, channel, tier, state,
outcome and route template are all small closed sets. Nothing here is labelled by
invoice, customer or experiment id - that belongs in traces and logs, where a high
cardinality costs nothing.
"""

from __future__ import annotations

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram

REGISTRY = CollectorRegistry(auto_describe=True)

# LLM latency ranges from ~200ms (small structured call) to tens of seconds (paced
# behind a rate limit), so the buckets span both.
_LLM_BUCKETS = (0.1, 0.25, 0.5, 1, 2, 4, 8, 15, 30, 60)

# --------------------------------------------------------------------------------------
# LLM client
# --------------------------------------------------------------------------------------

LLM_REQUESTS = Counter(
    "llm_requests_total",
    "LLM API attempts, by provider, model and result (ok, retry, failed).",
    ["provider", "model", "result"],
    registry=REGISTRY,
)
LLM_LATENCY = Histogram(
    "llm_request_duration_seconds",
    "Wall time of successful LLM API calls.",
    ["provider", "model"],
    buckets=_LLM_BUCKETS,
    registry=REGISTRY,
)
LLM_TOKENS = Counter(
    "llm_tokens_total",
    "Tokens reported by the provider, by kind (prompt, completion, reasoning).",
    ["provider", "model", "kind"],
    registry=REGISTRY,
)
LLM_FAILOVERS = Counter(
    "llm_failovers_total",
    "Times a request left a provider for the next one in the chain. A failover changes "
    "the model, which can confound an A/B result.",
    ["from_provider"],
    registry=REGISTRY,
)
LLM_PACING_WAIT = Counter(
    "llm_rate_limit_wait_seconds_total",
    "Seconds spent waiting for client-side token budget before sending.",
    ["provider", "model"],
    registry=REGISTRY,
)
LLM_TRUNCATED = Counter(
    "llm_truncated_replies_total",
    "Replies cut off by max_tokens or returned empty.",
    ["provider", "model"],
    registry=REGISTRY,
)
LLM_EXHAUSTED = Counter(
    "llm_all_providers_failed_total",
    "Requests that failed on every provider in the chain.",
    registry=REGISTRY,
)

# --------------------------------------------------------------------------------------
# Calls and call quality
# --------------------------------------------------------------------------------------

CALLS = Counter(
    "collections_calls_total",
    "Scored collections calls, by mode (voice, simulate), tier and extracted outcome.",
    ["mode", "tier", "outcome"],
    registry=REGISTRY,
)
CALL_COMPOSITE = Histogram(
    "collections_call_composite_score",
    "Judge composite score (1-5) per call, by prompt version.",
    ["prompt_id"],
    buckets=(1, 2, 2.5, 3, 3.5, 4, 4.5, 5),
    registry=REGISTRY,
)
CALL_HALLUCINATIONS = Counter(
    "collections_call_hallucinations_total",
    "Calls in which the agent stated a figure absent from the invoice.",
    ["prompt_id"],
    registry=REGISTRY,
)
CALL_COMPLIANCE = Counter(
    "collections_call_compliance_violations_total",
    "Calls the judge flagged for a compliance violation by the agent.",
    ["prompt_id"],
    registry=REGISTRY,
)
CAPTURE_DISAGREEMENTS = Counter(
    "collections_capture_disagreements_total",
    "Calls where the agent's live tool capture disagreed with transcript extraction.",
    registry=REGISTRY,
)

# --------------------------------------------------------------------------------------
# Dunning workflow
# --------------------------------------------------------------------------------------

DUNNING_CONTACTS = Counter(
    "dunning_contacts_total",
    "Customer contacts made by the workflow, by channel.",
    ["channel"],
    registry=REGISTRY,
)
DUNNING_TRANSITIONS = Counter(
    "dunning_transitions_total",
    "State transitions, by event kind (call, promise_broken, escalated, ...) and target.",
    ["event", "to_state"],
    registry=REGISTRY,
)
DUNNING_TASKS_OPENED = Counter(
    "dunning_tasks_opened_total",
    "Follow-up tasks opened, by type.",
    ["task_type"],
    registry=REGISTRY,
)
DUNNING_TICK_DURATION = Histogram(
    "dunning_tick_duration_seconds",
    "Wall time of one scheduler pass.",
    buckets=(0.05, 0.1, 0.5, 1, 2, 5, 10, 30, 60),
    registry=REGISTRY,
)
DUNNING_TICK_LAST_SUCCESS = Gauge(
    "dunning_tick_last_success_timestamp_seconds",
    "Unix time the last scheduler pass completed. Alert when this stops advancing.",
    registry=REGISTRY,
)
DUNNING_TICK_CONFLICTS = Counter(
    "dunning_tick_conflicts_total",
    "Invoices skipped in a pass because another writer changed them concurrently.",
    registry=REGISTRY,
)

# Point-in-time workflow state, refreshed from the database on each scrape.
DUNNING_INVOICES = Gauge(
    "dunning_invoices",
    "Invoices in the workflow, by state and risk tier.",
    ["state", "tier"],
    registry=REGISTRY,
)
DUNNING_OPEN_TASKS = Gauge(
    "dunning_open_tasks",
    "Open follow-up tasks, by team queue and type.",
    ["assignee", "task_type"],
    registry=REGISTRY,
)
DUNNING_OVERDUE_TASKS = Gauge(
    "dunning_overdue_tasks",
    "Open tasks past their due date, by team queue. This is the SLA breach count.",
    ["assignee"],
    registry=REGISTRY,
)
DUNNING_CALLS_DUE = Gauge(
    "dunning_calls_due",
    "Calls the cadence wants and the contact rules allow right now.",
    registry=REGISTRY,
)
DUNNING_OUTSTANDING = Gauge(
    "dunning_outstanding_inr",
    "Outstanding balance in active dunning, INR-normalised, by risk tier.",
    ["tier"],
    registry=REGISTRY,
)

# Cash application.
CASH_TRANSACTIONS = Counter(
    "cash_transactions_total",
    "Bank credits processed, by outcome (matched, unmatched, applied, dismissed) and the "
    "rule that decided it. matched / (matched + unmatched) is the auto-match rate.",
    ["status", "rule"],
    registry=REGISTRY,
)
CASH_UNMATCHED = Gauge(
    "cash_unmatched_transactions",
    "Bank credits waiting in the exceptions queue for a person.",
    registry=REGISTRY,
)

# --------------------------------------------------------------------------------------
# Experiments
# --------------------------------------------------------------------------------------

EXPERIMENT_SCENARIOS = Counter(
    "experiment_scenarios_completed_total",
    "A/B scenarios completed and persisted.",
    registry=REGISTRY,
)
EXPERIMENT_SCENARIO_ERRORS = Counter(
    "experiment_variant_errors_total",
    "Variant runs that errored within a scenario.",
    registry=REGISTRY,
)
EXPERIMENTS_FINISHED = Counter(
    "experiments_finished_total",
    "Experiments reaching a terminal state, by status.",
    ["status"],
    registry=REGISTRY,
)
EXPERIMENTS_ACTIVE = Gauge(
    "experiments_active",
    "Experiments currently running in this process.",
    registry=REGISTRY,
)

# --------------------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------------------

HTTP_REQUESTS = Counter(
    "http_requests_total",
    "HTTP requests, by method, route template and status code.",
    ["method", "route", "status"],
    registry=REGISTRY,
)
HTTP_LATENCY = Histogram(
    "http_request_duration_seconds",
    "HTTP request latency, by method and route template.",
    ["method", "route"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5),
    registry=REGISTRY,
)
