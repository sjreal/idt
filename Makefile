.PHONY: install check backend-check frontend-check test lint compose-config dev down model-pull guard-model-pull api-local frontend-local experiment garak-scan observability-up observability-down

MODEL ?= llama3.2:1b
GUARD_MODEL ?= llama-guard3:1b
GARAK_MODEL ?= longcat-2.5-preview-free
GARAK_LEVEL ?= off
GARAK_MAX_TOKENS ?= 512
SAMPLE_LIMIT ?= 42

install:
	uv sync --all-groups
	cd frontend && npm ci

check: backend-check frontend-check compose-config

backend-check: lint test

lint:
	uv run ruff check .

test:
	uv run pytest -q

frontend-check:
	cd frontend && npm ci && npm run build

compose-config:
	docker compose config --quiet

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
	uv run python -m security_eval.cli --seed 42 --sample-limit $(SAMPLE_LIMIT)

garak-scan:
	OPENAICOMPATIBLE_API_KEY=local-test uv run --project tools/garak --locked python -m garak \
		--target_type openai.OpenAICompatible \
		--target_name "$(GARAK_MODEL)" \
		--spec "probes.promptinject,probes.encoding" \
		--seed 42 --generations 1 \
		--generator_options '{"uri":"http://localhost:8000/v1/","temperature":0,"max_tokens":$(GARAK_MAX_TOKENS),"stop":[],"extra_params":{"guardrail_level":"$(GARAK_LEVEL)"}}'

observability-up:
	docker compose --profile observability up --build -d

observability-down:
	docker compose --profile observability stop grafana prometheus github-actions-exporter
