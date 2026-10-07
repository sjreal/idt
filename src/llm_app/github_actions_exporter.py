"""Poll public GitHub Actions metadata and expose bounded Prometheus metrics."""

from __future__ import annotations

import json
import logging
import os
import time
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from prometheus_client import Gauge, start_http_server

logger = logging.getLogger(__name__)
RUN_STATUSES = {
    "action_required",
    "completed",
    "in_progress",
    "pending",
    "queued",
    "requested",
    "waiting",
}
RUN_CONCLUSIONS = {
    "action_required",
    "cancelled",
    "failure",
    "neutral",
    "skipped",
    "stale",
    "startup_failure",
    "success",
    "timed_out",
}
RECENT_OUTCOMES = RUN_STATUSES | RUN_CONCLUSIONS | {"none", "other"}

EXPORTER_UP = Gauge(
    "llm_lab_github_actions_exporter_up",
    "Whether the latest GitHub Actions API poll succeeded.",
)
EXPORTER_LAST_SUCCESS_TIMESTAMP = Gauge(
    "llm_lab_github_actions_exporter_last_success_timestamp_seconds",
    "Unix time of the last successful GitHub Actions API poll.",
)
LATEST_RUN_STATUS = Gauge(
    "llm_lab_github_actions_latest_run_status",
    "One-hot status/conclusion for the latest workflow run.",
    ("workflow", "branch", "status", "conclusion"),
)
LATEST_RUN_TIMESTAMP = Gauge(
    "llm_lab_github_actions_latest_run_timestamp_seconds",
    "Unix timestamp when the latest workflow run was created.",
    ("workflow", "branch"),
)
LATEST_RUN_DURATION_SECONDS = Gauge(
    "llm_lab_github_actions_latest_run_duration_seconds",
    "Duration of the latest workflow run in seconds.",
    ("workflow", "branch"),
)
RECENT_RUNS = Gauge(
    "llm_lab_github_actions_recent_runs",
    "Workflow outcomes counted over the latest fetched run history.",
    ("workflow", "branch", "outcome"),
)


@dataclass(frozen=True)
class WorkflowRunSnapshot:
    workflow: str
    branch: str
    status: str
    conclusion: str
    created_at: float
    duration_seconds: float
    recent_outcomes: dict[str, int]


def _timestamp(value: object) -> float | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _bounded(value: object, allowed: set[str], missing: str = "other") -> str:
    return value if isinstance(value, str) and value in allowed else missing


def summarize_workflow_runs(
    runs: list[dict[str, object]],
    *,
    workflow: str,
    branch: str,
    now: float | None = None,
) -> WorkflowRunSnapshot | None:
    """Summarize runs without exposing run IDs as high-cardinality metric labels."""
    valid_runs = [run for run in runs if isinstance(run, dict)]
    if not valid_runs:
        return None

    current_time = time.time() if now is None else now
    latest = max(
        valid_runs,
        key=lambda run: _timestamp(run.get("created_at")) or 0,
    )
    status = _bounded(latest.get("status"), RUN_STATUSES)
    conclusion_value = latest.get("conclusion")
    conclusion = (
        _bounded(conclusion_value, RUN_CONCLUSIONS)
        if conclusion_value
        else "none"
    )
    created_at = _timestamp(latest.get("created_at")) or current_time
    started_at = _timestamp(latest.get("run_started_at")) or created_at
    if status == "completed":
        ended_at = (
            _timestamp(latest.get("updated_at"))
            or _timestamp(latest.get("completed_at"))
            or current_time
        )
    else:
        ended_at = current_time

    recent_outcomes: Counter[str] = Counter()
    for run in valid_runs:
        run_status = _bounded(run.get("status"), RUN_STATUSES)
        run_conclusion = run.get("conclusion")
        outcome = (
            _bounded(run_conclusion, RUN_CONCLUSIONS)
            if run_status == "completed" and run_conclusion
            else run_status
        )
        recent_outcomes[outcome] += 1

    return WorkflowRunSnapshot(
        workflow=workflow,
        branch=branch,
        status=status,
        conclusion=conclusion,
        created_at=created_at,
        duration_seconds=max(0.0, ended_at - started_at),
        recent_outcomes=dict(recent_outcomes),
    )


def publish_snapshot(snapshot: WorkflowRunSnapshot | None) -> None:
    if snapshot is None:
        LATEST_RUN_STATUS.clear()
        LATEST_RUN_TIMESTAMP.clear()
        LATEST_RUN_DURATION_SECONDS.clear()
        RECENT_RUNS.clear()
        return

    LATEST_RUN_STATUS.clear()
    LATEST_RUN_STATUS.labels(
        snapshot.workflow, snapshot.branch, snapshot.status, snapshot.conclusion
    ).set(1)
    LATEST_RUN_TIMESTAMP.labels(snapshot.workflow, snapshot.branch).set(snapshot.created_at)
    LATEST_RUN_DURATION_SECONDS.labels(snapshot.workflow, snapshot.branch).set(
        snapshot.duration_seconds
    )
    RECENT_RUNS.clear()
    for outcome in RECENT_OUTCOMES:
        RECENT_RUNS.labels(snapshot.workflow, snapshot.branch, outcome).set(
            snapshot.recent_outcomes.get(outcome, 0)
        )


def fetch_workflow_runs(
    repository: str,
    workflow_file: str,
    branch: str,
    token: str = "",
) -> list[dict[str, object]]:
    workflow = quote(workflow_file, safe="/")
    query = urlencode({"branch": branch, "per_page": 50})
    request = Request(
        f"https://api.github.com/repos/{repository}/actions/workflows/{workflow}/runs?{query}",
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "llm-guardrail-lab-ci-metrics/1.0",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    if token:
        request.add_header("Authorization", f"Bearer {token}")

    with urlopen(request, timeout=15) as response:
        data = json.load(response)
    runs = data.get("workflow_runs") if isinstance(data, dict) else None
    if not isinstance(runs, list):
        raise ValueError("GitHub Actions API returned no workflow_runs list")
    return [run for run in runs if isinstance(run, dict)]


def main() -> None:
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
    repository = os.getenv("GITHUB_REPOSITORY", "sjreal/idt").strip()
    workflow_file = os.getenv("GITHUB_ACTIONS_WORKFLOW_FILE", "ci.yml").strip()
    branch = os.getenv("GITHUB_ACTIONS_BRANCH", "main").strip()
    token = os.getenv("GITHUB_ACTIONS_TOKEN", "")
    poll_seconds = max(60, int(os.getenv("GITHUB_ACTIONS_POLL_SECONDS", "300")))
    port = int(os.getenv("GITHUB_ACTIONS_METRICS_PORT", "9101"))
    if repository.count("/") != 1 or not workflow_file or not branch:
        raise SystemExit("Set GITHUB_REPOSITORY=owner/repo and a workflow file/branch.")

    start_http_server(port, addr="0.0.0.0")
    logger.info(
        "Exporting GitHub Actions history for %s workflow=%s branch=%s",
        repository,
        workflow_file,
        branch,
    )
    while True:
        try:
            runs = fetch_workflow_runs(repository, workflow_file, branch, token)
            snapshot = summarize_workflow_runs(
                runs,
                workflow=workflow_file,
                branch=branch,
            )
            publish_snapshot(snapshot)
            EXPORTER_UP.set(1)
            EXPORTER_LAST_SUCCESS_TIMESTAMP.set(time.time())
        except (HTTPError, URLError, TimeoutError, ValueError, OSError):
            EXPORTER_UP.set(0)
            logger.exception("Could not poll GitHub Actions history")
        time.sleep(poll_seconds)


if __name__ == "__main__":
    main()
