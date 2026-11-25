"""The monitoring stack's config agrees with the metrics the code actually exports.

A renamed metric would otherwise leave a Grafana panel silently empty and an alert that
can never fire. These checks run without Docker.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import yaml

from shared.observability.metrics import REGISTRY

MONITORING = Path(__file__).resolve().parent.parent / "infra" / "monitoring"
OUR_METRIC = re.compile(r"\b(?:llm|dunning|collections|experiments?|http)_[a-z_]+\b")


def exported_names() -> set[str]:
    names: set[str] = set()
    for family in REGISTRY.collect():
        if family.type == "counter":
            names.add(f"{family.name}_total")
        elif family.type == "histogram":
            names |= {f"{family.name}_{s}" for s in ("bucket", "sum", "count")}
        else:
            names.add(family.name)
    return names


def alert_rules() -> list[dict]:
    raw = yaml.safe_load((MONITORING / "alerts.yml").read_text(encoding="utf-8"))
    return [rule for group in raw["groups"] for rule in group["rules"]]


def dashboard() -> dict:
    path = MONITORING / "grafana" / "dashboards" / "ar-collections.json"
    return json.loads(path.read_text(encoding="utf-8"))


def dashboard_exprs() -> list[str]:
    return [t["expr"] for p in dashboard()["panels"] for t in p.get("targets", [])]


def test_every_alert_uses_real_metrics() -> None:
    known = exported_names()
    for rule in alert_rules():
        missing = set(OUR_METRIC.findall(rule["expr"])) - known
        assert not missing, f"{rule['alert']} references unknown metrics {missing}"


def test_every_dashboard_panel_uses_real_metrics() -> None:
    known = exported_names()
    for expr in dashboard_exprs():
        missing = set(OUR_METRIC.findall(expr)) - known
        assert not missing, f"panel query references unknown metrics {missing}: {expr}"


def test_alerts_are_actionable() -> None:
    for rule in alert_rules():
        assert rule["labels"]["severity"] in {"warning", "critical"}, rule["alert"]
        assert rule["annotations"]["summary"] and rule["annotations"]["description"]


def test_every_series_panel_has_a_legend() -> None:
    for panel in dashboard()["panels"]:
        if panel["type"] == "timeseries":
            assert panel["options"]["legend"]["showLegend"], panel["title"]


def test_panels_do_not_overlap() -> None:
    occupied: set[tuple[int, int]] = set()
    for panel in dashboard()["panels"]:
        g = panel["gridPos"]
        cells = {
            (x, y) for x in range(g["x"], g["x"] + g["w"]) for y in range(g["y"], g["y"] + g["h"])
        }
        assert not cells & occupied, f"{panel['title']} overlaps another panel"
        assert g["x"] + g["w"] <= 24, f"{panel['title']} is wider than the grid"
        occupied |= cells


def test_prometheus_loads_the_alert_rules() -> None:
    raw = yaml.safe_load((MONITORING / "prometheus.yml").read_text(encoding="utf-8"))
    assert "/etc/prometheus/alerts.yml" in raw["rule_files"]
    assert raw["scrape_configs"][0]["metrics_path"] == "/metrics"
