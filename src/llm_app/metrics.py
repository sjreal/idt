"""Prometheus metrics for API, model, guardrail, and evaluation activity."""

from prometheus_client import Counter, Gauge, Histogram

HTTP_REQUESTS = Counter(
    "llm_lab_http_requests",
    "HTTP requests served by the LLM Lab API.",
    ("method", "endpoint", "status_code"),
)
HTTP_REQUEST_DURATION_SECONDS = Histogram(
    "llm_lab_http_request_duration_seconds",
    "HTTP request duration in seconds.",
    ("method", "endpoint"),
    buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 120, 300),
)
GENERATION_REQUESTS = Counter(
    "llm_lab_generation_requests",
    "Logical model-generation requests.",
    ("backend", "model", "outcome"),
)
GENERATION_REQUEST_DURATION_SECONDS = Histogram(
    "llm_lab_generation_request_duration_seconds",
    "Model-generation request duration in seconds, including retries.",
    ("backend", "model"),
    buckets=(0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 120, 180, 300, 360),
)
GENERATION_RETRIES = Counter(
    "llm_lab_generation_retries",
    "Retries performed for transient generation-provider failures.",
    ("backend", "model", "reason"),
)
GENERATION_TOKENS = Counter(
    "llm_lab_generation_tokens",
    "Prompt and completion tokens returned by generation backends.",
    ("backend", "model", "token_type"),
)
GUARDRAIL_CHECKS = Counter(
    "llm_lab_guardrail_checks",
    "Input and output guardrail checks.",
    ("level", "stage", "backend", "outcome"),
)
GUARDRAIL_CHECK_DURATION_SECONDS = Histogram(
    "llm_lab_guardrail_check_duration_seconds",
    "Wall-clock time spent in input/output guardrail checks.",
    ("level", "stage", "backend"),
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 120),
)
EVALUATION_RUNS = Counter(
    "llm_lab_evaluation_runs",
    "Evaluation runs completed by terminal status.",
    ("status",),
)
ACTIVE_EVALUATION_RUNS = Gauge(
    "llm_lab_evaluation_run_active",
    "Whether an evaluation run is currently executing (0 or 1).",
)
EVALUATION_CASES = Counter(
    "llm_lab_evaluation_cases",
    "Evaluation cases recorded by arm and case type.",
    ("arm", "kind", "outcome"),
)
