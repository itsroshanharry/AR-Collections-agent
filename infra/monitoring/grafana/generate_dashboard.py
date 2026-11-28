"""Generate the Grafana dashboard JSON. Dashboards as code: edit this, then run

    uv run python infra/monitoring/grafana/generate_dashboard.py

from the repo root and commit both files. tests/test_monitoring_config.py checks that
every query uses a metric the app actually exports.
"""

import json
from pathlib import Path

DS = {"type": "prometheus", "uid": "prometheus"}
panels = []
_id = 0
y = 0


def _next_id():
    global _id
    _id += 1
    return _id


def row(title):
    global y
    panels.append(
        {
            "type": "row",
            "title": title,
            "id": _next_id(),
            "collapsed": False,
            "gridPos": {"h": 1, "w": 24, "x": 0, "y": y},
            "panels": [],
        }
    )
    y += 1


def stat(title, expr, x, w, *, unit="none", thresholds=None, desc="", legend=""):
    steps = thresholds or [{"color": "text", "value": None}]
    return {
        "type": "stat",
        "title": title,
        "id": _next_id(),
        "description": desc,
        "datasource": DS,
        "gridPos": {"h": 4, "w": w, "x": x, "y": y},
        "targets": [{"refId": "A", "expr": expr, "legendFormat": legend, "datasource": DS}],
        "fieldConfig": {
            "defaults": {
                "unit": unit,
                "color": {"mode": "thresholds"},
                "thresholds": {"mode": "absolute", "steps": steps},
            },
            "overrides": [],
        },
        "options": {
            "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
            "colorMode": "value",
            "graphMode": "none",
            # Without a legend the "name" is the raw series labels, which only clutters a tile.
            "textMode": "value_and_name" if legend else "value",
            "justifyMode": "auto",
            "orientation": "auto",
        },
    }


def series(title, targets, x, w, *, unit="none", stack=False, desc="", h=8):
    return {
        "type": "timeseries",
        "title": title,
        "id": _next_id(),
        "description": desc,
        "datasource": DS,
        "gridPos": {"h": h, "w": w, "x": x, "y": y},
        "targets": [
            {"refId": chr(65 + i), "expr": e, "legendFormat": lf, "datasource": DS}
            for i, (e, lf) in enumerate(targets)
        ],
        "fieldConfig": {
            "defaults": {
                "unit": unit,
                "color": {"mode": "palette-classic"},
                "custom": {
                    "drawStyle": "line",
                    "lineWidth": 2,
                    "fillOpacity": 25 if stack else 0,
                    "showPoints": "never",
                    "spanNulls": True,
                    "stacking": {"mode": "normal" if stack else "none", "group": "A"},
                    "axisPlacement": "auto",
                    "gradientMode": "none",
                },
            },
            "overrides": [],
        },
        "options": {
            "legend": {"displayMode": "list", "placement": "bottom", "showLegend": True},
            "tooltip": {"mode": "multi", "sort": "desc"},
        },
    }


GOOD = {"color": "green", "value": None}

# ---------------------------------------------------------------- headline tiles
row("At a glance")
panels += [
    stat(
        "Calls due now",
        "dunning_calls_due",
        0,
        4,
        desc="Calls the cadence wants and the contact rules allow right now.",
    ),
    stat(
        "Overdue tasks (SLA breaches)",
        "sum(dunning_overdue_tasks) or vector(0)",
        4,
        5,
        thresholds=[GOOD, {"color": "red", "value": 1}],
        desc="Open follow-up tasks past their due date, all teams.",
    ),
    stat(
        "Outstanding in active dunning",
        "sum(dunning_outstanding_inr)",
        9,
        5,
        unit="currencyINR",
        desc="INR-normalised balance across every non-closed invoice.",
    ),
    stat(
        "Scheduler last pass",
        "time() - dunning_tick_last_success_timestamp_seconds",
        14,
        5,
        unit="s",
        thresholds=[GOOD, {"color": "orange", "value": 600}, {"color": "red", "value": 900}],
        desc="Seconds since the dunning scheduler last completed. Alert fires above 15 minutes.",
    ),
    stat(
        "Hallucination rate (24h)",
        # `or vector(0)`: the counter has no series until the first hallucination, and
        # without it the healthy case (none at all) would read "No data".
        "(sum(increase(collections_call_hallucinations_total[24h])) or vector(0)) / "
        "clamp_min(sum(increase(collections_calls_total[24h])), 1)",
        19,
        5,
        unit="percentunit",
        thresholds=[GOOD, {"color": "orange", "value": 0.02}, {"color": "red", "value": 0.05}],
        desc="Share of scored calls, A/B experiment calls included, in which the agent stated a "
        "figure absent from the invoice. The alert counts real calls only.",
    ),
]
y += 4

# ---------------------------------------------------------------- workflow
row("Dunning workflow")
panels += [
    series(
        "Invoices by state",
        [("sum by (state) (dunning_invoices)", "{{state}}")],
        0,
        12,
        stack=True,
        desc="Where the book stands: active dunning versus waiting on a person.",
    ),
    series(
        "Open tasks by team",
        [("sum by (assignee) (dunning_open_tasks)", "{{assignee}}")],
        12,
        12,
        stack=True,
        desc="Each team's work queue size.",
    ),
]
y += 8
panels += [
    series(
        "Contacts per hour by channel",
        [("sum by (channel) (increase(dunning_contacts_total[1h]))", "{{channel}}")],
        0,
        12,
        desc="Emails, SMS and calls actually made.",
    ),
    series(
        "Outstanding by risk tier",
        [("sum by (tier) (dunning_outstanding_inr)", "{{tier}}")],
        12,
        12,
        unit="currencyINR",
        stack=True,
    ),
]
y += 8

# ---------------------------------------------------------------- cash application
row("Cash application")
panels += [
    stat(
        "Auto-match rate (24h)",
        'sum(increase(cash_transactions_total{status="matched"}[24h])) / '
        'sum(increase(cash_transactions_total{status=~"matched|unmatched"}[24h]))',
        0,
        6,
        unit="percentunit",
        thresholds=[
            {"color": "red", "value": None},
            {"color": "orange", "value": 0.6},
            {"color": "green", "value": 0.8},
        ],
        desc="Share of bank credits applied to invoices with no person involved.",
    ),
    stat(
        "Unmatched payments waiting",
        "cash_unmatched_transactions",
        6,
        6,
        thresholds=[{"color": "green", "value": None}, {"color": "orange", "value": 10}],
        desc="The exceptions queue: credits a person has to apply or dismiss.",
    ),
]
y += 4
panels += [
    series(
        "Credits per hour by matching rule",
        [("sum by (rule) (increase(cash_transactions_total[1h]))", "{{rule}}")],
        0,
        24,
        stack=True,
        desc="Which rule decided each credit. 'none' is an exception; 'manual' a person.",
    ),
]
y += 8

# ---------------------------------------------------------------- call quality
row("Call quality")
panels += [
    series(
        "Calls per hour by outcome",
        [
            (
                "sum by (mode, outcome) (increase(collections_calls_total[1h]))",
                "{{mode}} / {{outcome}}",
            )
        ],
        0,
        12,
        stack=True,
    ),
    series(
        "Median judge score by prompt version",
        [
            (
                "histogram_quantile(0.5, sum by (le, prompt_id) "
                "(rate(collections_call_composite_score_bucket[6h])))",
                "{{prompt_id}}",
            )
        ],
        12,
        12,
        desc="Composite 1-5. A falling line is a prompt regression in production.",
    ),
]
y += 8
panels += [
    series(
        "Compliance violations and capture disagreements (per hour)",
        [
            (
                "sum(increase(collections_call_compliance_violations_total[1h])) or vector(0)",
                "compliance violations",
            ),
            (
                "sum(increase(collections_capture_disagreements_total[1h])) or vector(0)",
                "capture vs extraction disagreements",
            ),
        ],
        0,
        24,
        h=7,
        desc="Both should sit at zero. Either rising needs a person to read transcripts.",
    ),
]
y += 7

# ---------------------------------------------------------------- LLM
row("LLM providers")
panels += [
    series(
        "Requests per minute by result",
        [
            (
                "sum by (provider, result) (rate(llm_requests_total[5m])) * 60",
                "{{provider}} {{result}}",
            )
        ],
        0,
        8,
    ),
    series(
        "p95 latency by provider",
        [
            (
                "histogram_quantile(0.95, sum by (le, provider) "
                "(rate(llm_request_duration_seconds_bucket[10m])))",
                "{{provider}}",
            )
        ],
        8,
        8,
        unit="s",
    ),
    series(
        "Tokens per minute by kind",
        [("sum by (kind) (rate(llm_tokens_total[5m])) * 60", "{{kind}}")],
        16,
        8,
        desc="Reasoning tokens are billed against the same budget as the reply.",
    ),
]
y += 8
panels += [
    series(
        "Failovers per hour",
        [("sum by (from_provider) (increase(llm_failovers_total[1h]))", "from {{from_provider}}")],
        0,
        12,
        desc="A failover changes the model mid-experiment, which can confound an A/B.",
    ),
    series(
        "Rate-limit pacing wait (seconds per minute)",
        [("sum by (provider) (rate(llm_rate_limit_wait_seconds_total[5m])) * 60", "{{provider}}")],
        12,
        12,
        unit="s",
    ),
]
y += 8

# ---------------------------------------------------------------- platform
row("Platform")
panels += [
    series(
        "HTTP requests per second by status",
        [("sum by (status) (rate(http_requests_total[5m]))", "{{status}}")],
        0,
        12,
    ),
    series(
        "HTTP p95 latency by route",
        [
            (
                "histogram_quantile(0.95, sum by (le, route) "
                "(rate(http_request_duration_seconds_bucket[5m])))",
                "{{route}}",
            )
        ],
        12,
        12,
        unit="s",
    ),
]
y += 8
panels += [
    series(
        "Scheduler pass duration p95",
        [
            (
                "histogram_quantile(0.95, sum by (le) "
                "(rate(dunning_tick_duration_seconds_bucket[30m])))",
                "p95",
            )
        ],
        0,
        12,
        unit="s",
    ),
    series(
        "Experiments",
        [
            ("sum(experiments_active)", "running"),
            ("sum(increase(experiment_scenarios_completed_total[1h]))", "scenarios completed / h"),
        ],
        12,
        12,
    ),
]
y += 8

dashboard = {
    "uid": "ar-collections",
    "title": "AR Collections Agent",
    "description": "Dunning workflow, call quality, LLM providers and platform health.",
    "tags": ["ar-collections"],
    "timezone": "browser",
    "schemaVersion": 39,
    "version": 1,
    "refresh": "30s",
    "time": {"from": "now-6h", "to": "now"},
    "editable": True,
    "panels": panels,
    "templating": {"list": []},
    "annotations": {"list": []},
}
out = Path("infra/monitoring/grafana/dashboards/ar-collections.json")
out.parent.mkdir(parents=True, exist_ok=True)
out.write_text(json.dumps(dashboard, indent=2) + "\n", encoding="utf-8")
print(f"{len(panels)} panels -> {out}")
