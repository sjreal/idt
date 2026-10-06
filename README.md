# LLM CI/CD and Guardrail Evaluation

A locally runnable LLM application and reproducible study of open-source guardrails. The project compares an unguarded baseline with layered input/output protections and measures attack success, false refusals, and latency.

## Status

P0 scaffold. The application, model integration, guardrail experiment, and dashboard are planned follow-on phases.

## Requirements

- Python 3.12 (managed with [uv](https://docs.astral.sh/uv/))
- Node.js 22+ and npm
- Docker Engine with Docker Compose
- Git and GitHub CLI (`gh`) for repository operations

## Quick start

```sh
uv sync --all-groups
cd frontend && npm ci && npm run build
```

Copy `.env.example` to `.env` before starting local services. Do not put credentials in source control. Compose services and application commands will be added in the next phase.

## Checks

```sh
make check
docker compose config
```

## Research question

How much do layered open-source guardrails reduce prompt-injection and jailbreak attack success on an LLM application, and what are the costs in false refusals and latency?

See the project plan for scope, experiment arms, metrics, tools, limitations, and later phases.
