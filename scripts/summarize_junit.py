"""Write a concise pytest JUnit report into the GitHub Actions job summary."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from xml.etree import ElementTree


def _count(suites: list[ElementTree.Element], attribute: str) -> int:
    return sum(int(suite.get(attribute, "0")) for suite in suites)


def render_summary(report_path: Path, coverage_path: Path | None = None) -> str:
    if not report_path.exists():
        return (
            "## Python test results\n\n"
            "No JUnit report was generated. Check earlier setup/test steps.\n"
        )

    root = ElementTree.parse(report_path).getroot()
    suites = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))
    tests = _count(suites, "tests")
    failures = _count(suites, "failures")
    errors = _count(suites, "errors")
    skipped = _count(suites, "skipped")
    passed = max(0, tests - failures - errors - skipped)
    duration = sum(float(suite.get("time", "0")) for suite in suites)

    status = "✅ Passed" if failures == 0 and errors == 0 else "❌ Failed"
    lines = [
        "## Python test results",
        "",
        f"**{status}**",
        "",
        "| Total | Passed | Failed | Errors | Skipped | Duration |",
        "| ---: | ---: | ---: | ---: | ---: | ---: |",
        f"| {tests} | {passed} | {failures} | {errors} | {skipped} | {duration:.2f}s |",
    ]

    failed_cases = [
        case
        for case in root.iter("testcase")
        if case.find("failure") is not None or case.find("error") is not None
    ]
    if failed_cases:
        lines.extend(["", "### Failed tests", ""])
        for case in failed_cases:
            classname = case.get("classname", "")
            name = case.get("name", "unknown test")
            detail_node = case.find("failure")
            if detail_node is None:
                detail_node = case.find("error")
            detail_text = detail_node.get("message", "") + " " + (detail_node.text or "")
            detail = " ".join(detail_text.split())
            detail = detail.replace("|", "\\|")[:500]
            lines.append(f"- `{classname}.{name}` — {detail or 'see JUnit artifact'}")

    if coverage_path is not None and coverage_path.exists():
        coverage = ElementTree.parse(coverage_path).getroot()
        covered = int(coverage.get("lines-covered", "0"))
        valid = int(coverage.get("lines-valid", "0"))
        line_rate = float(coverage.get("line-rate", "0")) * 100
        lines.extend(
            [
                "",
                "## Python code coverage",
                "",
                f"**{line_rate:.1f}%** line coverage ({covered}/{valid} lines).",
            ]
        )

    return "\n".join(lines) + "\n"


def main() -> int:
    if len(sys.argv) not in {2, 3}:
        print("usage: summarize_junit.py REPORT.xml [COVERAGE.xml]", file=sys.stderr)
        return 2

    coverage_path = Path(sys.argv[2]) if len(sys.argv) == 3 else None
    summary = render_summary(Path(sys.argv[1]), coverage_path)
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with Path(summary_path).open("a", encoding="utf-8") as summary_file:
            summary_file.write(summary)
    else:
        print(summary, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
