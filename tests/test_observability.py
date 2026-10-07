import json
from pathlib import Path


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
    assert "Generation latency (p95, includes retries)" in titles
    assert "Evaluation active" in titles
    assert any("llm_lab_guardrail_checks_total" in query for query in queries)
