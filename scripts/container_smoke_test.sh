#!/usr/bin/env bash
set -euo pipefail

network="idt-ci-smoke"
api_container="idt-ci-smoke-api"
frontend_container="idt-ci-smoke-frontend"
api_url="http://127.0.0.1:18000"
frontend_url="http://127.0.0.1:13000"

cleanup() {
    docker rm -f "$frontend_container" "$api_container" >/dev/null 2>&1 || true
    docker network rm "$network" >/dev/null 2>&1 || true
}

trap cleanup EXIT

cleanup
docker network create "$network" >/dev/null

# An empty key makes /health report degraded without making a provider request.
docker run --detach \
    --name "$api_container" \
    --network "$network" \
    --network-alias api \
    --publish 127.0.0.1:18000:8000 \
    --env LLM_BACKEND=opencode-go \
    --env OPENCODE_GO_API_KEY= \
    --env GUARDRAIL_LEVEL=off \
    --env EVALUATION_RESULTS_DIR=/tmp/ci-smoke-results \
    idt-llm-cicd-api:ci >/dev/null

docker run --detach \
    --name "$frontend_container" \
    --network "$network" \
    --publish 127.0.0.1:13000:80 \
    idt-llm-cicd-frontend:ci >/dev/null

health_json=""
for attempt in $(seq 1 30); do
    if health_json=$(curl --fail --silent "$api_url/health" 2>/dev/null); then
        break
    fi
    sleep 2
done

if [[ -z "$health_json" ]]; then
    docker logs "$api_container"
    echo "API smoke container did not become ready" >&2
    exit 1
fi

python -c '
import json, sys
health = json.load(sys.stdin)
assert health["backend"] == "opencode-go", health
assert health["llm_connected"] is False, health
' <<<"$health_json"

metrics=$(curl --fail --silent "$api_url/metrics")
grep -q '^# HELP llm_lab_http_requests_total' <<<"$metrics"

summary_json=$(curl --fail --silent "$frontend_url/api/evaluations/summary")
python -c '
import json, sys
summary = json.load(sys.stdin)
assert summary["total"] == 42, summary
' <<<"$summary_json"

curl --fail --silent "$frontend_url/" >/dev/null
echo "Provider-free API/frontend smoke test passed."
