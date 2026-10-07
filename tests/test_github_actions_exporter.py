from llm_app.github_actions_exporter import summarize_workflow_runs


def test_github_actions_summary_counts_recent_outcomes_and_latest_duration() -> None:
    runs = [
        {
            "status": "completed",
            "conclusion": "failure",
            "created_at": "2026-10-07T10:00:00Z",
            "run_started_at": "2026-10-07T10:00:03Z",
            "updated_at": "2026-10-07T10:00:23Z",
        },
        {
            "status": "completed",
            "conclusion": "success",
            "created_at": "2026-10-07T09:00:00Z",
            "run_started_at": "2026-10-07T09:00:02Z",
            "updated_at": "2026-10-07T09:00:17Z",
        },
        {
            "status": "in_progress",
            "conclusion": None,
            "created_at": "2026-10-07T08:00:00Z",
            "run_started_at": "2026-10-07T08:00:02Z",
        },
    ]

    summary = summarize_workflow_runs(
        runs,
        workflow="ci.yml",
        branch="main",
        now=1_791_367_230,
    )

    assert summary is not None
    assert summary.status == "completed"
    assert summary.conclusion == "failure"
    assert summary.duration_seconds == 20
    assert summary.recent_outcomes == {
        "failure": 1,
        "success": 1,
        "in_progress": 1,
    }


def test_github_actions_summary_handles_empty_history() -> None:
    assert summarize_workflow_runs([], workflow="ci.yml", branch="main") is None
