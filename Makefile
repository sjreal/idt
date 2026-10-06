.PHONY: install check backend-check frontend-check test lint compose-config dev down model-pull api-local frontend-local

MODEL ?= llama3.2:1b

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

api-local:
	uv run uvicorn llm_app.main:app --reload --app-dir src --host 0.0.0.0 --port 8000

frontend-local:
	cd frontend && npm run dev
