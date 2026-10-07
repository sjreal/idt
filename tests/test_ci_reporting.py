import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "summarize_junit.py"


def test_junit_summary_counts_results_and_lists_failed_tests(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    report = tmp_path / "pytest-report.xml"
    report.write_text(
        '<testsuites><testsuite tests="3" failures="1" errors="0" skipped="1" time="1.25">'
        '<testcase classname="tests.test_api" name="test_ok" time="0.5" />'
        '<testcase classname="tests.test_api" name="test_failed" time="0.5">'
        '<failure message="assertion failed">expected true</failure></testcase>'
        '<testcase classname="tests.test_api" name="test_skipped"><skipped /></testcase>'
        "</testsuite></testsuites>",
        encoding="utf-8",
    )

    result = subprocess.run(
        [sys.executable, str(SCRIPT), str(report)], capture_output=True, text=True, check=True
    )

    assert "| 3 | 1 | 1 | 0 | 1 | 1.25s |" in result.stdout
    assert "`tests.test_api.test_failed`" in result.stdout
    assert "assertion failed expected true" in result.stdout


def test_junit_summary_handles_missing_report(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    result = subprocess.run(
        [sys.executable, str(SCRIPT), str(tmp_path / "missing.xml")],
        capture_output=True,
        text=True,
        check=True,
    )

    assert "No JUnit report was generated" in result.stdout
