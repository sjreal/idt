import json
from pathlib import Path

import yaml


def test_grafana_dashboard_is_provisioned_with_live_project_metrics() -> None:
    dashboard_path = (
        Path(__file__).resolve().parents[1]
        / "observability"
        / "grafana"
        / "dashboards"
        / "llm-guardrail-lab.json"
    )
    dashboard = json.loads(dashboard_path.read_text(encoding="utf-8"))
    panels = dashboard["panels"]
    titles = {panel["title"] for panel in panels}
    queries = {
        target["expr"]
        for panel in panels
        for target in panel.get("targets", [])
    }

    assert dashboard["uid"] == "llm-guardrail-lab-live"
    assert "API request rate" in titles
    assert "API latency (p50 / p95 / p99)" in titles
    assert "API process CPU" in titles
    assert "API process memory" in titles
    assert "Generation failures by model (1h)" in titles
    assert "Generation retries (1h)" in titles
    assert "Guardrail latency by mode and stage (p95)" in titles
    assert "Evaluation errors by arm (1h)" in titles
    assert "Evaluation runs by terminal status (24h)" in titles
    assert "Latest GitHub Actions run" in titles
    assert "Evaluation active" in titles
    assert any("llm_lab_guardrail_checks_total" in query for query in queries)
    assert any("llm_lab_github_actions_recent_runs" in query for query in queries)
    assert any("ALERTS" in query for query in queries)


def test_prometheus_scrapes_api_and_github_actions_exporter_and_loads_alerts() -> None:
    root = Path(__file__).resolve().parents[1] / "observability"
    config = yaml.safe_load((root / "prometheus.yml").read_text(encoding="utf-8"))
    alerts = yaml.safe_load((root / "alerts.yml").read_text(encoding="utf-8"))

    targets = {
        target
        for job in config["scrape_configs"]
        for scrape_group in job["static_configs"]
        for target in scrape_group["targets"]
    }
    rules = [rule for group in alerts["groups"] for rule in group["rules"]]

    assert targets == {"api:8000", "github-actions-exporter:9101"}
    alert_names = {rule["alert"] for rule in rules}
    # Original alerts must remain; SLO/burn-rate alerts are additive.
    assert {
        "LlmApiTargetDown",
        "LlmApiElevated5xxRate",
        "LlmGenerationProviderFailures",
    } <= alert_names
    assert {
        "ChatAvailabilityFastBurn",
        "ChatAvailabilitySlowBurn",
        "GuardrailUnavailable",
        "EvaluationRunAborted",
        "CircuitOpenExtended",
        "FallbackRateHigh",
    } <= alert_names


def test_guardrail_alert_does_not_fire_on_an_absent_metric() -> None:
    """The baseline arm runs no stage, so no guardrail series exist.

    An ``absent()`` clause would fire on a healthy system that only used the
    ``off`` arm. The alert must key off the error ratio alone.
    """
    root = Path(__file__).resolve().parents[1] / "observability"
    alerts = yaml.safe_load((root / "alerts.yml").read_text(encoding="utf-8"))
    rules = {
        rule["alert"]: rule
        for group in alerts["groups"]
        for rule in group["rules"]
    }

    expr = rules["GuardrailUnavailable"]["expr"]
    assert "absent(" not in expr
    assert "llm_lab_guardrail_checks_total" in expr
