.PHONY: install check backend-check frontend-check test lint compose-config

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
