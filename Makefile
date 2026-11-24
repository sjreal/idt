.PHONY: install check backend-check frontend-check test lint compose-config dev down model-pull guard-model-pull api-local frontend-local experiment calibrate garak-scan observability-up observability-down phoenix-up traces pricing-refresh pricing-check report compare data-check

MODEL ?= llama3.2:1b
GUARD_MODEL ?= llama-guard3:1b
GARAK_MODEL ?= longcat-2.5-preview-free
GARAK_LEVEL ?= off
GARAK_MAX_TOKENS ?= 512
SAMPLE_LIMIT ?= 42
ARMS ?= off scanners full
REPLICATES ?= 1
# Leave empty to use the server default (60). Raising it is an explicit override.
MAX_REQUESTS ?=
RUN_ID ?=
A ?=
B ?=

install:
	uv sync --all-groups
	cd frontend && npm ci

check: backend-check frontend-check compose-config

backend-check: lint test pricing-check data-check

lint:
	uv run ruff check .

test:
	uv run pytest -q

frontend-check:
	cd frontend && npm ci && npm run build

compose-config:
	docker compose config --quiet
	docker compose -f docker-compose.yml -f docker-compose.observability.yml --profile observability config --quiet

dev:
	docker compose up --build -d

down:
	docker compose down

model-pull:
	docker compose exec ollama ollama pull "$(MODEL)"

guard-model-pull:
	docker compose exec ollama ollama pull "$(GUARD_MODEL)"

api-local:
	uv run uvicorn llm_app.main:app --reload --app-dir src --host 0.0.0.0 --port 8000

frontend-local:
	cd frontend && npm run dev

experiment:
	uv run python -m security_eval.cli --seed 42 --sample-limit $(SAMPLE_LIMIT) \
		--arms $(ARMS) --replicates $(REPLICATES) \
		$(if $(MAX_REQUESTS),--max-requests $(MAX_REQUESTS),)

calibrate:
	uv run python scripts/calibrate_adaptive.py \
		$(if $(CALIBRATION_MODEL),--model $(CALIBRATION_MODEL),) \
		$(if $(CALIBRATION_ARMS),--arms $(CALIBRATION_ARMS),)

observability-up:
	docker compose -f docker-compose.yml -f docker-compose.observability.yml --profile observability up --build -d

observability-down:
	docker compose -f docker-compose.yml -f docker-compose.observability.yml --profile observability stop grafana prometheus github-actions-exporter phoenix

phoenix-up:
	docker compose -f docker-compose.yml -f docker-compose.observability.yml --profile observability up -d phoenix

traces:
	@curl -fsS http://localhost:8000/api/traces?limit=20 || echo "API not reachable; local traces are in traces/ (if TRACE_DIR was overridden)."

pricing-refresh:
	uv run python scripts/refresh_pricing.py --write

pricing-check:
	uv run python -c "from llm_app.pricing import get_catalog; c = get_catalog(); print('pricing snapshot', c.sha256[:12], 'ok')"

data-check:
	uv run python -c "from pathlib import Path; from security_eval.dataset import load_dataset; paths = sorted(Path('data/evaluation').glob('*.manifest.json')) + [Path('data/evaluation/manifest.json')]; [print(p.name, len(load_dataset(p).cases), 'cases ok') for p in paths]"

garak-scan:
	OPENAICOMPATIBLE_API_KEY=local-test uv run --project tools/garak --locked python -m garak \
		--target_type openai.OpenAICompatible \
		--target_name "$(GARAK_MODEL)" \
		--spec "probes.promptinject,probes.encoding" \
		--seed 42 --generations 1 \
		--generator_options '{"uri":"http://localhost:8000/v1/","temperature":0,"max_tokens":$(GARAK_MAX_TOKENS),"stop":[],"extra_params":{"guardrail_level":"$(GARAK_LEVEL)"}}'

report:
	@test -n "$(RUN_ID)" || (echo "Usage: make report RUN_ID=<run-id>" && exit 1)
	@curl -fsS "http://localhost:8000/api/evaluations/runs/$(RUN_ID)/report.html" -o "evaluation-$(RUN_ID).html"
	@echo "Wrote evaluation-$(RUN_ID).html"

compare:
	@test -n "$(A)" -a -n "$(B)" || (echo "Usage: make compare A=<run-id> B=<run-id>" && exit 1)
	@echo "http://localhost:8000/api/reports/compare.html?run_ids=$(A)&run_ids=$(B)"
