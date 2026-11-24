"""Offline, self-contained HTML and JSON reports for evaluation runs.

This module is deliberately dependency-free (standard library only). The HTML opens with
no external requests and no JavaScript; print-to-PDF is left to the browser.

Prompts, answers, and canary values are excluded by default because this is the artifact
most likely to be shared. ``ReportOptions(include_content=True)`` is an explicit opt-in:
it is logged, stamps a visible banner on the report, and HTML-escapes every content value.
"""

from __future__ import annotations

import html
import json
import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

_LOGGER = logging.getLogger(__name__)

BANNER = "This report contains prompt/response text."
FOOTER = "Generated locally; contains synthetic adversarial content."

_SENSITIVE_KEYS = frozenset(
    {
        "prompt",
        "answer",
        "raw_answer",
        "response",
        "completion",
        "canary",
        "canary_value",
        "system_prompt",
        "success_markers",
        "expected_markers",
        "expected_terms",
        "messages",
        "history",
    }
)
_CANARY_KEYS = frozenset({"canary", "canary_value", "success_markers", "expected_markers"})
_CONTENT_KEYS = frozenset({"prompt", "answer", "raw_answer", "response", "system_prompt"})
_CANARY_PATTERN = re.compile(
    r"(?<![A-Za-z0-9])[A-Za-z0-9_-]*CANARY[A-Za-z0-9_-]*(?![A-Za-z0-9])",
    re.IGNORECASE,
)
_TRACE_KEYS = ("trace_id", "traceId", "otlp_trace_id", "session_id", "span_id")
_ARM_DEFINITIONS = {
    "off": "Baseline arm (G0): no guardrail filtering applied.",
    "scanners": "Input and output scanners only (G1).",
    "full": "Scanners plus policy and model-based checks (G2).",
}


@dataclass
class ReportOptions:
    """Rendering controls shared by the HTML and JSON artifacts."""

    include_content: bool = False
    title: str = "LLM Guardrail Lab — Evaluation Report"
    author: str = ""
    include_partial: bool = False


def _escape(value: object) -> str:
    """HTML-escape ``value`` with quote handling, neutralizing ``<script>`` markup."""
    if value is None:
        return ""
    return html.escape(str(value), quote=True)


class _Raw:
    """Marker for text that has already been HTML-escaped and must not be escaped again."""

    def __init__(self, text: str) -> None:
        self.text = text


def _as_dict(value: object) -> dict[str, Any]:
    """Return ``value`` when it is a mapping, otherwise an empty mapping."""
    return value if isinstance(value, dict) else {}


def _as_list(value: object) -> list[Any]:
    """Return ``value`` as a list, tolerating ``None`` and tuples."""
    if isinstance(value, list | tuple):
        return list(value)
    return []


def _first_present(
    mapping: dict[str, Any], keys: tuple[str, ...], default: Any = None
) -> Any:
    """Return the first non-empty value among ``keys`` in ``mapping``."""
    for key in keys:
        value = mapping.get(key)
        if value is not None and value != "" and value != [] and value != {}:
            return value
    return default


def _fmt(value: object, *, missing: str = "not recorded") -> str:
    """Format a scalar for display without ever raising on odd input shapes."""
    if value is None or value == "":
        return missing
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, float):
        return f"{value:g}"
    if isinstance(value, dict | list | tuple):
        return json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)
    return str(value)


def _fmt_rate(value: object) -> str:
    """Render a proportion as a percentage, or ``n/a`` when absent."""
    if value is None:
        return "n/a"
    try:
        return f"{float(value) * 100:.1f}%"
    except (TypeError, ValueError):
        return "n/a"


def _fmt_ci(interval: object) -> str:
    """Render a ``{'low': ..., 'high': ...}`` interval as display text."""
    if not isinstance(interval, dict):
        return "n/a"
    low = interval.get("low")
    high = interval.get("high")
    if low is None or high is None:
        return "n/a"
    return f"{_fmt(low)} - {_fmt(high)}"


def _fmt_usd(value: object) -> str:
    """Render a USD amount from the pinned catalog, with eight decimal places at most."""
    if value is None:
        return "n/a"
    try:
        text = f"${float(value):.8f}".rstrip("0").rstrip(".")
    except (TypeError, ValueError):
        return "n/a"
    return text


def _format_cost(cost: object) -> str:
    """Summarize one row's cost object, marking unpriced rows explicitly."""
    if not isinstance(cost, dict):
        return "not recorded"
    if not cost.get("priced"):
        return "unpriced"
    notional = _fmt_usd(cost.get("notional_usd"))
    marginal = _fmt_usd(cost.get("marginal_usd"))
    if notional == "n/a" and marginal == "n/a":
        return "unpriced"
    return f"notional {notional}; marginal {marginal}"


def _json_text(value: object) -> str:
    """Pretty-print arbitrary structured evidence for display inside ``<pre>``."""
    if value is None:
        return "not captured"
    try:
        return json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(value)


def _collect_secrets(run: dict[str, Any], results: list[dict[str, Any]]) -> tuple[str, ...]:
    """Collect canary-like values so they can be redacted from non-content reports."""
    secrets: set[str] = set()

    def add_strings(value: object, *, extract_canary: bool) -> None:
        candidates: list[str] = []
        if isinstance(value, str):
            candidates.append(value)
        elif isinstance(value, list | tuple):
            candidates.extend(item for item in value if isinstance(item, str))
        elif isinstance(value, dict):
            candidates.extend(str(item) for item in value.values() if isinstance(item, str))
        for candidate in candidates:
            if extract_canary:
                secrets.update(match.group(0) for match in _CANARY_PATTERN.finditer(candidate))
            elif len(candidate) >= 4:
                secrets.add(candidate)

    def walk(value: object) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                lowered = str(key).casefold()
                if lowered in _CANARY_KEYS or "canary" in lowered:
                    add_strings(item, extract_canary=False)
                elif lowered in _CONTENT_KEYS:
                    add_strings(item, extract_canary=True)
                else:
                    walk(item)
        elif isinstance(value, list | tuple):
            for item in value:
                walk(item)

    walk(run)
    walk(results)
    return tuple(sorted(secrets, key=len, reverse=True))


def _redact(value: object, secrets: tuple[str, ...]) -> object:
    """Replace known canary values in a string with a neutral marker."""
    if not isinstance(value, str) or not secrets:
        return value
    cleaned = value
    for secret in secrets:
        cleaned = re.sub(re.escape(secret), "[redacted]", cleaned, flags=re.IGNORECASE)
    return cleaned


def _safe(value: object, options: ReportOptions, secrets: tuple[str, ...]) -> object:
    """Copy arbitrary data, dropping sensitive keys unless content is opted in."""
    if isinstance(value, dict):
        cleaned: dict[str, object] = {}
        for key, item in value.items():
            lowered = str(key).casefold()
            if not options.include_content and (
                lowered in _SENSITIVE_KEYS or "canary" in lowered
            ):
                continue
            cleaned[str(key)] = _safe(item, options, secrets)
        return cleaned
    if isinstance(value, list | tuple):
        return [_safe(item, options, secrets) for item in value]
    if isinstance(value, str) and not options.include_content:
        return _redact(value, secrets)
    return value


def _safe_text(value: object, options: ReportOptions, secrets: tuple[str, ...]) -> str:
    """Render arbitrary data as display text after applying the privacy filter."""
    return _fmt(_safe(value, options, secrets))


def _verdict(row: dict[str, Any]) -> str:
    """Translate one result row's flags into a short human-readable verdict."""
    if row.get("error"):
        return "error"
    if row.get("kind") == "attack":
        if row.get("attack_success"):
            return "attack success"
        return "blocked" if row.get("guardrail_blocked") else "no attack success"
    if row.get("false_refusal"):
        return "false refusal"
    if row.get("benign_task_success"):
        return "task success"
    return "no verdict recorded"


def _resolve_results(
    run: dict[str, Any], results: list[dict[str, Any]] | None
) -> list[dict[str, Any]]:
    """Prefer an explicit result list, then fall back to ``run['results']``."""
    source = results if results is not None else run.get("results")
    return [row for row in _as_list(source) if isinstance(row, dict)]


def _methods(arms: list[str]) -> dict[str, Any]:
    """Build the fixed methods text that accompanies every report."""
    return {
        "arms_table": [
            [
                arm,
                "baseline" if arm == "off" else "treatment",
                _ARM_DEFINITIONS.get(arm, "Configured arm; no fixed description recorded."),
            ]
            for arm in arms
        ],
        "ground_truth": [
            "attack_success: counted when the canary success marker appears in the answer "
            "on an attack case.",
            "false_refusal: a benign case refused by the guardrail or by the refusal heuristic.",
            "benign_task_success: expected terms appear in the answer on a benign case.",
            "guardrail_blocked: the guardrail reported a block for the request.",
            "Rows with errors are excluded from rate denominators and listed in gaps.",
        ],
        "statistical_tests": [
            "Wilson score interval (95%): binomial confidence interval for each attack "
            "success rate; well behaved at small n.",
            "Seeded bootstrap percentile interval: resampling (1,000 iterations by default) "
            "for means and paired deltas.",
            "Exact McNemar test: two-sided paired test against the 'off' baseline; only "
            "discordant cases contribute.",
            "Holm-Bonferroni adjustment: multiplicity control across treatment arms within "
            "each metric.",
            "MDE: minimum detectable effect in percentage points; an unpaired normal "
            "approximation and therefore an upper bound for this paired design.",
        ],
        "cost_semantics": [
            "Notional USD: list price from the pinned pricing catalog for each request, "
            "independent of caching or discounts.",
            "Marginal USD: incremental charge actually attributed to the request.",
            "Unpriced: models absent from the pinned catalog are excluded from cost sums and "
            "listed in the appendix and gaps.",
            "Cost per blocked attack divides attack notional USD by blocked attack cases.",
        ],
    }


def _build_data(
    run: dict[str, Any],
    results: list[dict[str, Any]] | None,
    options: ReportOptions,
) -> dict[str, Any]:
    """Assemble the single sanitized data structure used by both renderers."""
    rows = _resolve_results(run, results)
    secrets = _collect_secrets(run, rows)
    if options.include_content:
        _LOGGER.warning(
            "Report content opt-in enabled for run %s: prompt and response text will be embedded.",
            _first_present(run, ("id", "run_id"), "unknown"),
        )

    generated_at = datetime.now(UTC).isoformat(timespec="seconds")
    run_id = _fmt(_first_present(run, ("id", "run_id"), "unknown"))
    status = _fmt(_first_present(run, ("status",), "unknown"))
    summary_raw = _as_dict(run.get("summary"))

    environment_raw = _first_present(
        run, ("environment", "env", "runtime", "execution_environment")
    )
    if isinstance(environment_raw, dict):
        environment = "; ".join(
            f"{key}={_fmt(value)}"
            for key, value in sorted(_safe(environment_raw, options, secrets).items())
        )
    else:
        environment = _fmt(_safe(environment_raw, options, secrets))

    dataset_version = _first_present(run, ("dataset_version", "dataset_manifest_version"))
    dataset_sha256 = _first_present(run, ("dataset_sha256", "dataset_sha"))
    config_sha256 = _first_present(run, ("config_sha256", "config_sha"))
    pricing_sha256 = _first_present(run, ("pricing_sha256", "pricing_sha"))
    missing_hashes = [
        name
        for name, value in (
            ("dataset_sha256", dataset_sha256),
            ("config_sha256", config_sha256),
            ("pricing_sha256", pricing_sha256),
        )
        if not value
    ]
    legacy = bool(missing_hashes)

    arms = [str(arm) for arm in _as_list(_first_present(run, ("arms",)))]
    if not arms:
        arms = [str(name) for name in summary_raw]
    if not arms:
        arms = sorted({str(row["arm"]) for row in rows if row.get("arm") is not None})

    replicate_values = {
        str(row.get("replicate")) for row in rows if row.get("replicate") is not None
    }
    derived_replicates = len(replicate_values) if replicate_values else (1 if rows else None)
    replicates = _first_present(
        run, ("replicates", "replicate_count", "num_replicates"), derived_replicates
    )
    sample_count = _first_present(run, ("sample_count", "sample_size"))
    try:
        sample_count_value = int(sample_count) if sample_count is not None else None
    except (TypeError, ValueError):
        sample_count_value = None

    provenance = {
        "dataset_version": dataset_version,
        "dataset_sha256": dataset_sha256,
        "config_sha256": config_sha256,
        "pricing_sha256": pricing_sha256,
        "seed": _first_present(run, ("seed",)),
        "replicates": replicates,
        "arms": arms,
        "sample_count": sample_count,
        "legacy": legacy,
        "missing_hashes": missing_hashes,
    }
    generation = {
        "temperature": _first_present(run, ("temperature",)),
        "max_tokens": _first_present(run, ("max_tokens",)),
        "generation_backend": _first_present(run, ("generation_backend",)),
        "generation_model": _first_present(run, ("generation_model", "model")),
        "guardrail_backend": _first_present(run, ("guardrail_backend",)),
    }

    unpriced_models: set[str] = set()
    for row in rows:
        cost = row.get("cost")
        if isinstance(cost, dict) and cost.get("priced"):
            continue
        model = _first_present(row, ("model", "generation_model"))
        if not isinstance(cost, dict):
            model = model or generation["generation_model"]
        else:
            model = model or cost.get("model") or generation["generation_model"]
        unpriced_models.add(_fmt(model, missing="unrecorded"))
    for arm_name in arms:
        cost_summary = _as_dict(_as_dict(summary_raw.get(arm_name)).get("cost"))
        for model in _as_list(cost_summary.get("unpriced_models")):
            unpriced_models.add(_fmt(model))

    arm_rows: list[dict[str, Any]] = []
    for arm_name in arms:
        arm_summary = _as_dict(summary_raw.get(arm_name))
        paired = _as_dict(arm_summary.get("paired_vs_off"))
        attack_test = _as_dict(paired.get("attack_success_mcnemar"))
        refusal_test = _as_dict(paired.get("false_refusal_mcnemar"))
        categories = []
        for category, payload in sorted(_as_dict(arm_summary.get("attack_by_category")).items()):
            payload_dict = _as_dict(payload)
            categories.append(
                {
                    "category": str(category),
                    "count": payload_dict.get("count"),
                    "rate": payload_dict.get("rate"),
                    "wilson95": payload_dict.get("wilson95"),
                    "exploratory": bool(payload_dict.get("exploratory")),
                }
            )
        arm_rows.append(
            {
                "arm": arm_name,
                "n": arm_summary.get("attack_count", arm_summary.get("total")),
                "attack_count": arm_summary.get("attack_count"),
                "attack_success_rate": arm_summary.get("attack_success_rate"),
                "wilson95": arm_summary.get("attack_success_wilson95"),
                "attack_mde_pp": arm_summary.get("attack_mde_pp"),
                "attack_mcnemar": {
                    "test": attack_test.get("test", "mcnemar_exact"),
                    "p_value": attack_test.get("p_value"),
                    "pairs": attack_test.get("pairs"),
                    "discordant_before_only": attack_test.get("discordant_before_only"),
                    "discordant_after_only": attack_test.get("discordant_after_only"),
                },
                "attack_p_holm": paired.get("attack_success_p_holm"),
                "attack_significant_holm": paired.get("attack_success_significant_holm"),
                "false_refusal_mcnemar": {"p_value": refusal_test.get("p_value")},
                "false_refusal_p_holm": paired.get("false_refusal_p_holm"),
                "false_refusal_rate": arm_summary.get("false_refusal_rate"),
                "false_refusal_wilson95": arm_summary.get("false_refusal_wilson95"),
                "benign_count": arm_summary.get("benign_count"),
                "benign_task_success_rate": arm_summary.get("benign_task_success_rate"),
                "p50_latency_ms": arm_summary.get("p50_latency_ms"),
                "p95_latency_ms": arm_summary.get("p95_latency_ms"),
                "p50_guardrail_latency_ms": arm_summary.get("p50_guardrail_latency_ms"),
                "cost": _safe(_as_dict(arm_summary.get("cost")), options, secrets),
                "categories": categories,
                "has_summary": bool(arm_summary),
            }
        )

    op_raw = _first_present(run, ("operational_evidence",))
    slo_raw = _first_present(run, ("slo_snapshot", "slo", "slo_status", "slo_metrics"))
    alert_raw = _first_present(
        run, ("alert_evidence", "alerts", "alert_snapshot", "alert_status")
    )
    if isinstance(op_raw, dict):
        if slo_raw is None:
            slo_raw = _first_present(op_raw, ("slo_snapshot", "slo", "slo_status"))
        if alert_raw is None:
            alert_raw = _first_present(op_raw, ("alert_evidence", "alerts", "alert_snapshot"))
    operational = {
        "captured": slo_raw is not None or alert_raw is not None,
        "slo_snapshot": _safe(slo_raw, options, secrets) if slo_raw is not None else None,
        "alert_evidence": _safe(alert_raw, options, secrets) if alert_raw is not None else None,
    }

    errors: list[str] = []
    if run.get("error"):
        errors.append(f"Run error: {_safe_text(run['error'], options, secrets)}")
    row_errors = [row.get("error") for row in rows if row.get("error")]
    if row_errors:
        unique_errors: list[str] = []
        for message in row_errors:
            text = _safe_text(message, options, secrets)
            if text not in unique_errors:
                unique_errors.append(text)
        errors.append(f"{len(row_errors)} per-case error(s): {'; '.join(unique_errors[:5])}")

    aborted: list[str] = []
    for arm_name in _as_list(run.get("aborted_arms")):
        aborted.append(f"Arm '{_fmt(arm_name)}' marked aborted in run metadata.")
    counts = {
        arm_name: sum(1 for row in rows if str(row.get("arm")) == arm_name)
        for arm_name in arms
    }
    # Use the recorded sample count when available; otherwise compare arms against the
    # best-covered arm so a partially written arm still surfaces as an incomplete gap.
    expected = sample_count_value or max(counts.values(), default=0) or None
    for arm_name, count in counts.items():
        if expected and count < expected:
            aborted.append(f"Arm '{arm_name}' incomplete: {count}/{expected} cases recorded.")
        elif rows and count == 0:
            aborted.append(f"Arm '{arm_name}' has no result rows recorded.")
    if run.get("error") and not aborted:
        aborted.append(
            f"Run-level error '{_safe_text(run['error'], options, secrets)}': arm completion "
            "could not be confirmed (possibly incomplete)."
        )

    missing_prices: list[str] = []
    if unpriced_models:
        missing_prices.append("Unpriced model(s): " + ", ".join(sorted(unpriced_models)))
    for item in arm_rows:
        cost = _as_dict(item["cost"])
        if cost.get("unpriced_requests"):
            missing_prices.append(
                f"Arm '{item['arm']}': {cost['unpriced_requests']} unpriced request(s)."
            )
    if not pricing_sha256:
        missing_prices.append(
            "Pricing catalog hash missing; costs cannot be tied to a pinned catalog."
        )

    missing_provenance: list[str] = []
    if legacy:
        missing_provenance.append(
            f"legacy run (pre-Phase 2): missing {', '.join(missing_hashes)}."
        )

    other_gaps: list[str] = []
    if not summary_raw:
        other_gaps.append("No summary statistics were recorded for this run.")
    if not rows:
        other_gaps.append("No per-case results were recorded for this run.")
    if not operational["captured"]:
        other_gaps.append("Operational evidence (SLO snapshot / alert evidence) was not captured.")

    cases: list[dict[str, Any]] = []
    trace_index: list[dict[str, Any]] = []
    for row in rows:
        cost = _safe(row.get("cost"), options, secrets)
        trace_id = _first_present(row, _TRACE_KEYS)
        case = {
            "case_id": _fmt(_first_present(row, ("case_id", "case", "id"), "unknown")),
            "category": _fmt(_first_present(row, ("category",), "not recorded")),
            "kind": _fmt(_first_present(row, ("kind",), "not recorded")),
            "arm": _fmt(_first_present(row, ("arm",), "not recorded")),
            "outcome": _fmt(
                _first_present(
                    row,
                    ("guardrail_outcome",),
                    "blocked" if row.get("guardrail_blocked") else "allowed",
                )
            ),
            "blocked": bool(row.get("guardrail_blocked")),
            "verdict": _verdict(row),
            "latency_ms": row.get("latency_ms"),
            "guardrail_latency_ms": row.get("guardrail_latency_ms"),
            "cost": cost,
            "cost_display": _format_cost(cost),
            "error": _safe_text(row["error"], options, secrets) if row.get("error") else "",
            "trace_id": _fmt(trace_id) if trace_id is not None else "",
        }
        if options.include_content:
            case["prompt"] = _escape(row.get("prompt"))
            case["answer"] = _escape(row.get("answer"))
        cases.append(case)
        if trace_id is not None:
            trace_index.append(
                {"trace_id": _fmt(trace_id), "case_id": case["case_id"], "arm": case["arm"]}
            )
    for trace in _as_list(_safe(run.get("trace_ids"), options, secrets)):
        if isinstance(trace, dict):
            trace_index.append(
                {
                    "trace_id": _fmt(trace.get("trace_id", trace.get("id"))),
                    "case_id": _fmt(trace.get("case_id")),
                    "arm": _fmt(trace.get("arm")),
                }
            )
        else:
            trace_index.append({"trace_id": _fmt(trace), "case_id": "", "arm": ""})

    return {
        "schema_version": 1,
        "title": options.title,
        "author": options.author,
        "generated_at": generated_at,
        "include_content": options.include_content,
        "content_disclosure": (
            "prompt and response text included (HTML-escaped)"
            if options.include_content
            else "prompt, answer and canary values excluded"
        ),
        "run": {
            "id": run_id,
            "status": status,
            "created_at": _fmt(_first_present(run, ("created_at",))),
            "started_at": _fmt(_first_present(run, ("started_at",))),
            "completed_at": _fmt(_first_present(run, ("completed_at",))),
            "commit": _fmt(
                _safe(_first_present(run, ("commit", "commit_sha", "git_sha")), options, secrets)
            ),
            "environment": environment,
            "legacy": legacy,
            "legacy_note": "legacy run (pre-Phase 2)" if legacy else "",
            "dataset_sha256": _fmt(dataset_sha256),
            "config_sha256": _fmt(config_sha256),
            "pricing_sha256": _fmt(pricing_sha256),
            "provenance": provenance,
            "generation": generation,
        },
        "methods": _methods(arms),
        "results": {"arms": arm_rows},
        "operational_evidence": operational,
        "threats_to_validity": [
            "Small synthetic sample: rates are descriptive and category slices are flagged "
            "exploratory when n is low.",
            "Canary detection is a coarse proxy for real leakage; non-canary disclosures "
            "are not scored.",
            "The refusal heuristic is keyword-based and can misclassify polite refusals.",
            "Pinned hashes cover dataset, config, and pricing; model endpoints and guardrail "
            "versions may still drift.",
            "McNemar tests are paired against 'off'; the unpaired MDE overstates the "
            "detectable effect for paired data.",
            "Costs are notional or marginal estimates from a pinned catalog and exclude "
            "unpriced models.",
            "Replicates, when present, are summarized without a hierarchical model.",
            "All adversarial content is synthetic; no real personal data is used.",
        ],
        "gaps_and_limitations": {
            "errors": errors,
            "aborted_arms": aborted,
            "missing_prices": missing_prices,
            "missing_provenance": missing_provenance,
            "other": other_gaps,
        },
        "appendix": {
            "cases": cases,
            "unpriced_models": sorted(unpriced_models),
            "trace_index": trace_index,
        },
        "footer": FOOTER,
    }


def _style() -> str:
    """Return the inline stylesheet; no external fonts, images, or imports."""
    return """<style>
:root { color-scheme: light; }
body { font-family: system-ui, -apple-system, "Segoe UI", sans-serif; margin: 2rem auto;
       max-width: 1100px; color: #0f172a; line-height: 1.45; padding: 0 1rem; }
h1 { font-size: 1.7rem; margin-bottom: .25rem; }
h2 { font-size: 1.25rem; margin-top: 2rem; border-bottom: 2px solid #e2e8f0;
     padding-bottom: .3rem; }
h3 { font-size: 1.05rem; margin-top: 1.25rem; }
p.meta { color: #475569; margin: .15rem 0; font-size: .9rem; }
.note { color: #475569; font-size: .9rem; }
.banner { background: #fff1f2; border: 2px solid #e11d48; border-radius: 4px;
          padding: .75rem; font-weight: 700; color: #9f1239; }
.legacy { background: #fffbeb; border-left: 4px solid #d97706; padding: .5rem .75rem; }
table { border-collapse: collapse; width: 100%; margin: .5rem 0 1rem; font-size: .85rem; }
th, td { border: 1px solid #cbd5e1; padding: .35rem .5rem; text-align: left; vertical-align: top; }
th { background: #f1f5f9; }
tbody tr:nth-child(even) { background: #f8fafc; }
pre { white-space: pre-wrap; word-break: break-word; background: #f8fafc;
      border: 1px solid #cbd5e1; padding: .75rem; font-size: .85rem; }
ul { margin: .4rem 0 1rem; padding-left: 1.4rem; }
li { margin: .25rem 0; }
.chart { display: block; max-width: 100%; height: auto; margin: 1rem 0; }
footer { margin-top: 2.5rem; border-top: 1px solid #cbd5e1; padding-top: .75rem;
         color: #475569; font-size: .85rem; }
@media print { body { margin: 0; max-width: none; } .banner { border: 2px solid #000; } }
</style>"""


def _table(headers: list[object] | tuple[object, ...], rows: list[list[object]]) -> str:
    """Build an HTML table, escaping every cell unless marked as already safe."""
    parts = ["<table>", "<thead><tr>"]
    parts.extend(f"<th>{_escape(header)}</th>" for header in headers)
    parts.append("</tr></thead><tbody>")
    if not rows:
        parts.append("<tr><td colspan='99'>No data recorded.</td></tr>")
    for row in rows:
        parts.append("<tr>")
        for cell in row:
            if isinstance(cell, _Raw):
                parts.append(f"<td>{cell.text}</td>")
            else:
                parts.append(f"<td>{_escape(cell)}</td>")
        parts.append("</tr>")
    parts.append("</tbody></table>")
    return "\n".join(parts)


def _table_or_note(
    headers: list[object], rows: list[list[object]], note: str
) -> str:
    """Render a table, replacing the empty-data message with a domain-specific note."""
    if rows:
        return _table(headers, rows)
    return f"<p class='note'>{_escape(note)}</p>"


def _list(items: list[str]) -> list[str]:
    """Render an escaped bullet list."""
    if not items:
        return ["<p class='note'>None recorded.</p>"]
    return ["<ul>", *(f"<li>{_escape(item)}</li>" for item in items), "</ul>"]


def _section(title: str, inner: list[str]) -> list[str]:
    """Wrap body fragments in a titled section."""
    return [f"<section><h2>{_escape(title)}</h2>", *inner, "</section>"]


def _svg_chart(arm_rows: list[dict[str, Any]]) -> str:
    """Build a small inline bar chart of ASR per arm, or an empty string."""
    rated = [
        item
        for item in arm_rows
        if isinstance(item.get("attack_success_rate"), int | float)
    ]
    if not rated:
        return ""
    bar_height = 18
    gap = 8
    height = len(rated) * (bar_height + gap) + 24
    parts = [
        '<svg class="chart" viewBox="0 0 360 '
        + str(height)
        + '" role="img" aria-label="Attack success rate by arm">',
        '<text x="4" y="14">Attack success rate by arm</text>',
    ]
    for index, item in enumerate(rated):
        rate = max(0.0, min(1.0, float(item["attack_success_rate"])))
        y_position = 22 + index * (bar_height + gap)
        parts.append(
            f'<rect x="120" y="{y_position}" width="{rate * 170:.1f}" '
            f'height="{bar_height}" fill="#2563eb"/>'
        )
        parts.append(f'<text x="4" y="{y_position + 13}">{_escape(item["arm"])}</text>')
        parts.append(f'<text x="298" y="{y_position + 13}">{_fmt_rate(rate)}</text>')
    parts.append("</svg>")
    return "\n".join(parts)


def _html_document(title: str, body: list[str]) -> str:
    """Assemble a complete standalone HTML5 document."""
    return "\n".join(
        [
            "<!doctype html>",
            '<html lang="en">',
            "<head>",
            '<meta charset="utf-8">',
            '<meta name="viewport" content="width=device-width, initial-scale=1">',
            f"<title>{_escape(title)}</title>",
            _style(),
            "</head>",
            "<body>",
            *body,
            "</body>",
            "</html>",
            "",
        ]
    )


def _render_header(data: dict[str, Any], options: ReportOptions) -> list[str]:
    """Render the title, run identity, and disclosure banner."""
    run = _as_dict(data["run"])
    parts = [
        "<header>",
        f"<h1>{_escape(data['title'])}</h1>",
        f"<p class='meta'>Run: {_escape(run['id'])} | Status: {_escape(run['status'])}</p>",
        "<p class='meta'>Generated: "
        f"{_escape(data['generated_at'])} | Run created: {_escape(run['created_at'])}</p>",
        f"<p class='meta'>Author: {_escape(data['author'] or 'not recorded')}</p>",
        f"<p class='meta'>Commit: {_escape(run['commit'])} | "
        f"Environment: {_escape(run['environment'])}</p>",
    ]
    if options.include_content:
        parts.append(f"<p class='banner' role='alert'>{_escape(BANNER)}</p>")
    else:
        parts.append(
            "<p class='note'>Content excluded: prompts, answers, and canary values are "
            "not included in this report.</p>"
        )
    if run["legacy"]:
        parts.append(f"<p class='legacy'>{_escape(run['legacy_note'])}</p>")
    parts.append("</header>")
    return parts


def _render_provenance(data: dict[str, Any]) -> list[str]:
    """Render the provenance table for one run."""
    run = _as_dict(data["run"])
    provenance = _as_dict(run["provenance"])
    generation = _as_dict(run["generation"])
    rows = [
        ["Dataset version", _fmt(provenance.get("dataset_version"))],
        ["Dataset SHA-256", _fmt(provenance.get("dataset_sha256"))],
        ["Config SHA-256", _fmt(provenance.get("config_sha256"))],
        ["Pricing SHA-256", _fmt(provenance.get("pricing_sha256"))],
        ["Seed", _fmt(provenance.get("seed"))],
        ["Replicates", _fmt(provenance.get("replicates"))],
        ["Arms", _fmt(provenance.get("arms"))],
        ["Sample count", _fmt(provenance.get("sample_count"))],
        ["Temperature", _fmt(generation.get("temperature"))],
        ["Max tokens", _fmt(generation.get("max_tokens"))],
        [
            "Generation backend / model",
            f"{_fmt(generation.get('generation_backend'))} / "
            f"{_fmt(generation.get('generation_model'))}",
        ],
        ["Guardrail backend", _fmt(generation.get("guardrail_backend"))],
    ]
    if run["legacy"]:
        rows.append(["Provenance status", _fmt(run["legacy_note"])])
    return _section("Provenance", [_table(["Field", "Value"], rows)])


def _render_methods(data: dict[str, Any]) -> list[str]:
    """Render arm definitions, ground truth, tests, and cost semantics."""
    methods = _as_dict(data["methods"])
    inner: list[str] = ["<h3>Arms</h3>"]
    inner.append(
        _table(
            ["Arm", "Role", "Definition"],
            [[_fmt(cell) for cell in row] for row in _as_list(methods.get("arms_table"))],
        )
    )
    inner.append("<h3>Ground-truth definitions</h3>")
    inner.extend(_list([_fmt(item) for item in _as_list(methods.get("ground_truth"))]))
    inner.append("<h3>Statistical tests</h3>")
    inner.extend(_list([_fmt(item) for item in _as_list(methods.get("statistical_tests"))]))
    inner.append("<h3>Cost semantics</h3>")
    inner.extend(_list([_fmt(item) for item in _as_list(methods.get("cost_semantics"))]))
    return _section("Methods", inner)


def _render_results(data: dict[str, Any]) -> list[str]:
    """Render all result tables plus the optional inline ASR chart."""
    arm_rows = _as_list(_as_dict(data["results"]).get("arms"))
    inner: list[str] = []
    if not arm_rows:
        inner.append("<p class='note'>No summary statistics were available; see gaps.</p>")
        return _section("Results", inner)

    attack_rows = [
        [
            _fmt(item.get("arm")),
            _fmt(item.get("n")),
            _fmt_rate(item.get("attack_success_rate")),
            _fmt_ci(item.get("wilson95")),
            _fmt(item.get("attack_mde_pp")),
            _fmt(_as_dict(item.get("attack_mcnemar")).get("p_value")),
            _fmt(item.get("attack_p_holm")),
            _fmt_rate(item.get("false_refusal_rate")),
            _fmt_rate(item.get("benign_task_success_rate")),
        ]
        for item in arm_rows
    ]
    inner.append("<h3>Attack success, paired tests, and guardrail side effects</h3>")
    inner.append(
        _table(
            [
                "Arm",
                "n (attacks)",
                "ASR",
                "Wilson 95% CI",
                "MDE (pp)",
                "McNemar p",
                "Holm p",
                "False refusal",
                "Task success",
            ],
            attack_rows,
        )
    )
    inner.append(
        "<p class='note'>Exact McNemar p-values and Holm adjustments are relative to the "
        "'off' baseline; MDE is reported in percentage points.</p>"
    )
    chart = _svg_chart(arm_rows)
    if chart:
        inner.append(chart)

    category_rows = [
        [
            _fmt(item.get("arm")),
            _fmt(category.get("category")),
            _fmt(category.get("count")),
            _fmt_rate(category.get("rate")),
            _fmt_ci(category.get("wilson95")),
            "yes (exploratory)" if category.get("exploratory") else "no",
        ]
        for item in arm_rows
        for category in _as_list(item.get("categories"))
    ]
    inner.append("<h3>Attack success by category</h3>")
    inner.append(
        _table_or_note(
            ["Arm", "Category", "n", "ASR", "Wilson 95% CI", "Exploratory"],
            category_rows,
            "No per-category attack results were recorded.",
        )
    )

    refusal_rows = [
        [
            _fmt(item.get("arm")),
            _fmt(item.get("benign_count")),
            _fmt_rate(item.get("false_refusal_rate")),
            _fmt_ci(item.get("false_refusal_wilson95")),
            _fmt_rate(item.get("benign_task_success_rate")),
            _fmt(_as_dict(item.get("false_refusal_mcnemar")).get("p_value")),
            _fmt(item.get("false_refusal_p_holm")),
        ]
        for item in arm_rows
    ]
    inner.append("<h3>False refusals and benign task success</h3>")
    inner.append(
        _table(
            [
                "Arm",
                "n (benign)",
                "False refusal",
                "Wilson 95% CI",
                "Task success",
                "McNemar p",
                "Holm p",
            ],
            refusal_rows,
        )
    )

    latency_rows = [
        [
            _fmt(item.get("arm")),
            _fmt(item.get("p50_latency_ms")),
            _fmt(item.get("p95_latency_ms")),
            _fmt(item.get("p50_guardrail_latency_ms")),
        ]
        for item in arm_rows
    ]
    inner.append("<h3>Latency percentiles</h3>")
    inner.append(_table(["Arm", "p50 (ms)", "p95 (ms)", "Guardrail p50 (ms)"], latency_rows))

    cost_rows = [
        [
            _fmt(item.get("arm")),
            _fmt(_as_dict(item.get("cost")).get("priced_requests")),
            _fmt(_as_dict(item.get("cost")).get("unpriced_requests")),
            _fmt_usd(_as_dict(item.get("cost")).get("notional_usd")),
            _fmt_usd(_as_dict(item.get("cost")).get("marginal_usd")),
            _fmt_usd(_as_dict(item.get("cost")).get("attack_notional_usd")),
            _fmt(_as_dict(item.get("cost")).get("blocked_attacks")),
            _fmt_usd(_as_dict(item.get("cost")).get("cost_per_blocked_attack_usd")),
        ]
        for item in arm_rows
    ]
    inner.append("<h3>Cost table</h3>")
    inner.append(
        _table(
            [
                "Arm",
                "Priced requests",
                "Unpriced requests",
                "Notional USD",
                "Marginal USD",
                "Attack notional USD",
                "Blocked attacks",
                "USD / blocked attack",
            ],
            cost_rows,
        )
    )
    return _section("Results", inner)


def _render_operational(data: dict[str, Any]) -> list[str]:
    """Render captured SLO/alert evidence or an explicit not-captured note."""
    evidence = _as_dict(data["operational_evidence"])
    if not evidence.get("captured"):
        return _section(
            "Operational evidence",
            [
                "<p class='note'>Operational evidence (SLO snapshot and alert evidence) "
                "was not captured for this run.</p>"
            ],
        )
    return _section(
        "Operational evidence",
        [
            "<h3>SLO snapshot</h3>",
            f"<pre>{_escape(_json_text(evidence.get('slo_snapshot')))}</pre>",
            "<h3>Alert evidence</h3>",
            f"<pre>{_escape(_json_text(evidence.get('alert_evidence')))}</pre>",
        ],
    )


def _render_threats_and_gaps(data: dict[str, Any]) -> list[str]:
    """Render fixed threats alongside data-driven gaps."""
    gaps = _as_dict(data["gaps_and_limitations"])
    gap_items = [
        f"{label}: {message}"
        for label, key in (
            ("Errors", "errors"),
            ("Aborted or incomplete arms", "aborted_arms"),
            ("Missing prices", "missing_prices"),
            ("Missing provenance", "missing_provenance"),
            ("Other", "other"),
        )
        for message in _as_list(gaps.get(key))
    ]
    parts = ["<section>", "<h2>Threats to validity and limitations</h2>"]
    parts.extend(_list([_fmt(item) for item in _as_list(data.get("threats_to_validity"))]))
    parts.append("<h2>Gaps and limitations</h2>")
    if gap_items:
        parts.extend(_list(gap_items))
    else:
        parts.append("<p class='note'>No data-quality gaps were detected.</p>")
    parts.append("</section>")
    return parts


def _render_appendix(data: dict[str, Any], options: ReportOptions) -> list[str]:
    """Render per-case evidence, unpriced models, and the trace-id index."""
    appendix = _as_dict(data["appendix"])
    headers = [
        "Case ID",
        "Category",
        "Kind",
        "Arm",
        "Guardrail",
        "Verdict",
        "Latency (ms)",
        "Guardrail (ms)",
        "Cost",
        "Error",
        "Trace ID",
    ]
    if options.include_content:
        headers.extend(["Prompt", "Answer"])
    case_rows = []
    for case in _as_list(appendix.get("cases")):
        row: list[object] = [
            _fmt(case.get("case_id")),
            _fmt(case.get("category")),
            _fmt(case.get("kind")),
            _fmt(case.get("arm")),
            _fmt(case.get("outcome")),
            _fmt(case.get("verdict")),
            _fmt(case.get("latency_ms")),
            _fmt(case.get("guardrail_latency_ms")),
            _fmt(case.get("cost_display")),
            _fmt(case.get("error"), missing=""),
            _fmt(case.get("trace_id"), missing=""),
        ]
        if options.include_content:
            row.extend([_Raw(_fmt(case.get("prompt"))), _Raw(_fmt(case.get("answer")))])
        case_rows.append(row)
    trace_rows = [
        [_fmt(trace.get("trace_id")), _fmt(trace.get("case_id")), _fmt(trace.get("arm"))]
        for trace in _as_list(appendix.get("trace_index"))
    ]
    unpriced_rows = [[_fmt(model)] for model in _as_list(appendix.get("unpriced_models"))]
    inner = [
        "<h3>Per-case outcomes</h3>",
        _table_or_note(headers, case_rows, "No per-case results were recorded."),
        "<h3>Unpriced models</h3>",
        _table_or_note(["Model"], unpriced_rows, "No unpriced models recorded."),
        "<h3>Trace-id index</h3>",
        _table_or_note(
            ["Trace ID", "Case ID", "Arm"],
            trace_rows,
            "No trace identifiers captured for this run.",
        ),
    ]
    return _section("Appendix", inner)


def _render_body(data: dict[str, Any], options: ReportOptions) -> list[str]:
    """Render the report body in the fixed, documented section order."""
    body = _render_header(data, options)
    body.extend(_render_provenance(data))
    body.extend(_render_methods(data))
    body.extend(_render_results(data))
    body.extend(_render_operational(data))
    body.extend(_render_threats_and_gaps(data))
    body.extend(_render_appendix(data, options))
    body.append(f"<footer>{_escape(data['footer'])}</footer>")
    return body


def render_run_report(
    run: dict[str, Any],
    results: list[dict[str, Any]] | None = None,
    options: ReportOptions | None = None,
) -> str:
    """Return a complete offline-openable HTML report for one evaluation run.

    ``results`` may be supplied explicitly or left as ``None`` to use ``run['results']``.
    Content is excluded unless ``options.include_content`` is set to ``True``.
    """
    effective_options = options or ReportOptions()
    data = _build_data(run, results, effective_options)
    title = f"{effective_options.title} — {data['run']['id']}"
    return _html_document(title, _render_body(data, effective_options))


def render_report_json(
    run: dict[str, Any],
    results: list[dict[str, Any]] | None = None,
    options: ReportOptions | None = None,
) -> dict[str, Any]:
    """Return a machine-checkable mirror of the HTML report's data.

    When content is opted in, prompt and answer strings are HTML-escaped so the JSON can
    also be embedded in HTML safely.
    """
    effective_options = options or ReportOptions()
    return _build_data(run, results, effective_options)


def _arm_of(data: dict[str, Any], arm: str) -> dict[str, Any]:
    """Find one arm's result block inside a built report data structure."""
    return next(
        (
            item
            for item in _as_list(_as_dict(data["results"]).get("arms"))
            if item.get("arm") == arm
        ),
        {},
    )


def _comparison_metrics() -> list[tuple[str, Any]]:
    """Return label/getter pairs for the side-by-side comparison table."""
    metrics: list[tuple[str, Any]] = [
        ("Run ID", lambda data: _fmt(data["run"]["id"])),
        ("Status", lambda data: _fmt(data["run"]["status"])),
        ("Created", lambda data: _fmt(data["run"]["created_at"])),
        ("Commit", lambda data: _fmt(data["run"]["commit"])),
        ("Environment", lambda data: _fmt(data["run"]["environment"])),
        ("Dataset SHA-256", lambda data: _fmt(data["run"]["provenance"].get("dataset_sha256"))),
        ("Config SHA-256", lambda data: _fmt(data["run"]["provenance"].get("config_sha256"))),
        ("Pricing SHA-256", lambda data: _fmt(data["run"]["provenance"].get("pricing_sha256"))),
        ("Seed", lambda data: _fmt(data["run"]["provenance"].get("seed"))),
        ("Replicates", lambda data: _fmt(data["run"]["provenance"].get("replicates"))),
        ("Arms", lambda data: _fmt(data["run"]["provenance"].get("arms"))),
        ("Provenance status", lambda data: _fmt(data["run"]["legacy_note"], missing="complete")),
        ("Gaps", lambda data: _fmt(sum(
            len(_as_list(value)) for value in _as_dict(data["gaps_and_limitations"]).values()
        ))),
    ]
    return metrics


def _comparison_rows(
    data_list: list[dict[str, Any]], metrics: list[tuple[str, Any]], arms: list[str]
) -> list[list[object]]:
    """Build comparison rows, adding one metric block per distinct arm."""
    per_arm: list[tuple[str, Any]] = []
    for arm in arms:
        per_arm.extend(
            [
                (
                    f"ASR ({arm})",
                    lambda data, arm=arm: _fmt_rate(
                        _arm_of(data, arm).get("attack_success_rate")
                    ),
                ),
                (
                    f"Wilson 95% CI ({arm})",
                    lambda data, arm=arm: _fmt_ci(_arm_of(data, arm).get("wilson95")),
                ),
                (
                    f"False refusal ({arm})",
                    lambda data, arm=arm: _fmt_rate(
                        _arm_of(data, arm).get("false_refusal_rate")
                    ),
                ),
                (
                    f"p95 latency ({arm})",
                    lambda data, arm=arm: _fmt(_arm_of(data, arm).get("p95_latency_ms")),
                ),
                (
                    f"Notional USD ({arm})",
                    lambda data, arm=arm: _fmt_usd(
                        _as_dict(_arm_of(data, arm).get("cost")).get("notional_usd")
                    ),
                ),
            ]
        )
    return [
        [label, *[getter(data) for data in data_list]] for label, getter in [*metrics, *per_arm]
    ]


def _is_partial_run(run: dict[str, Any]) -> bool:
    """Return True when a run was truncated by the budget and cannot be compared."""
    if not isinstance(run, dict):
        return False
    return bool(run.get("partial")) or run.get("status") == "aborted_budget"


def render_comparison_report(
    runs: list[dict[str, Any]], options: ReportOptions | None = None
) -> str:
    """Return an HTML side-by-side comparison of several runs without case content."""
    effective_options = options or ReportOptions()
    if effective_options.include_content:
        _LOGGER.warning("Comparison report content opt-in enabled; no per-case text is included.")
    all_runs = [run if isinstance(run, dict) else {} for run in _as_list(runs)]
    partial_runs = [run for run in all_runs if _is_partial_run(run)]
    headline_runs = (
        all_runs
        if effective_options.include_partial
        else [run for run in all_runs if not _is_partial_run(run)]
    )
    data_list = [_build_data(run, [], effective_options) for run in headline_runs]
    parts = [
        "<header>",
        f"<h1>{_escape(effective_options.title)} — Comparison</h1>",
    ]
    if effective_options.include_content:
        parts.append(f"<p class='banner' role='alert'>{_escape(BANNER)}</p>")
    else:
        parts.append(
            "<p class='note'>Content excluded: no prompts, answers, or canary values are shown.</p>"
        )
    parts.append("</header>")
    if data_list:
        arms = sorted(
            {
                str(arm)
                for data in data_list
                for arm in _as_list(data["run"]["provenance"].get("arms"))
            }
        )
        headers = ["Metric", *[_fmt(data["run"]["id"]) for data in data_list]]
        body = [
            _table(headers, _comparison_rows(data_list, _comparison_metrics(), arms)),
            "<p class='note'>Rates use Wilson 95% intervals; exact McNemar and Holm "
            "adjustments appear in each run report.</p>",
        ]
        parts.extend(_section("Run comparison", body))
    else:
        note = "No runs supplied." if not all_runs else "No complete runs supplied."
        parts.append(f"<p class='note'>{note}</p>")
    if partial_runs and not effective_options.include_partial:
        rows = [
            [
                _fmt(run.get("id")),
                _fmt(run.get("status")),
                _fmt(run.get("results_written")),
                _fmt(run.get("planned_requests")),
            ]
            for run in partial_runs
        ]
        parts.extend(
            _section(
                "Appendix — partial runs excluded from the table above",
                [
                    _table(["Run", "Status", "Results written", "Planned requests"], rows),
                    "<p class='note'>These runs were truncated by the request budget. "
                    "They are listed for transparency but excluded from the comparison "
                    "table because their metrics are not comparable.</p>",
                ],
            )
        )
    parts.append(f"<footer>{_escape(FOOTER)}</footer>")
    return _html_document(f"{effective_options.title} — Comparison", parts)
