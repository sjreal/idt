# LLM Guardrail Lab

A local LLM playground and reproducible study of open-source guardrails. The planned experiment compares an unguarded baseline with layered input/output protections and measures attack success, false refusals, and latency.

## Status

**P1 complete:** FastAPI chat API, Ollama and optional OpenCode Go backends, OpenAI-compatible chat-completions route, React chat UI, tests, and Compose services. Assistant replies render sanitized GitHub-style Markdown. The UI simulates word-by-word typing after receiving the complete API response; HTTP streaming is not implemented yet. Guardrails and the experiment harness are follow-on phases.

## Stack

- Python 3.12, FastAPI, Uvicorn, HTTPX, managed and locked with [uv](https://docs.astral.sh/uv/)
- React, TypeScript, Vite, Tailwind CSS, shadcn/ui, React Markdown
- Ollama with a local model, or an optional OpenCode Go API backend
- Docker Compose for the local stack

## Requirements

- Docker Desktop (macOS) or Docker Engine with Docker Compose
- `make` (preinstalled with Xcode Command Line Tools on macOS)
- For direct host development only: Python 3.12 with uv, Node.js 22+, npm

On macOS, start Docker Desktop and wait for it to finish starting before using Compose. Confirm the engine is available with:

```sh
docker info
```

## First-time setup

From the repository root, create your private local settings file. This file is gitignored; do not commit API keys.

```sh
cp .env.example .env
```

Choose **one** model backend below by editing `.env`, then start the stack. The browser UI is at <http://localhost:3000>; the API is at <http://localhost:8000>.

### Option A: Ollama (local inference)

In `.env`, use:

```dotenv
LLM_BACKEND=ollama
COMPOSE_PROFILES=ollama
OLLAMA_MODEL=llama3.2:1b
```

Start the API, frontend, and Ollama container, then download the model into Ollama's persistent Docker volume:

```sh
make dev
make model-pull
```

The first pull can take a while and needs disk space. To choose another Ollama model, change `OLLAMA_MODEL` in `.env`, then run `make dev` and `make model-pull MODEL=your-model-name`.

### Option B: OpenCode Go (remote inference)

OpenCode Go requires a Go subscription and an API key from the OpenCode Console. The default model below is listed as free for a limited time; Go usage/model limits still apply. A low-cost non-preview Chat Completions option is `glm-5.3-flash`. Parameter counts are not published, so the smallest model cannot be verified. See the [OpenCode Go guide](https://opencode.ai/v2/console/go) for current models, compatibility, limits, and usage guidance.

In `.env`, use your own key (do not paste it into source files or commit it):

```dotenv
LLM_BACKEND=opencode-go
COMPOSE_PROFILES=
OPENCODE_GO_MODEL=longcat-2.5-preview-free
OPENCODE_GO_API_KEY=your-key-from-the-opencode-console
```

Then start the stack:

```sh
make dev
```

No Ollama model pull is needed in Go mode. If an Ollama container was already started, stop that unused service with `docker compose stop ollama`. The app sends a custom `User-Agent` and a stable `x-opencode-session` header for each chat. Go is intended for coding-agent-style traffic; make sure your use follows its current terms and guidance.

## Start, update, inspect, and stop

`make dev` runs `docker compose up --build -d`. You can run it again after changing source or dependencies: Compose rebuilds changed images and recreates affected containers. It is safe to rerun while the stack is already up.

Check what is running and inspect logs:

```sh
docker compose ps
docker compose logs -f api frontend
```

Check the backend and configured model status:

```sh
curl http://localhost:8000/health
```

To **stop and remove the containers/network but keep downloaded models and database data**:

```sh
make down
```

To stop containers but leave them created so you can restart them quickly:

```sh
docker compose stop
docker compose start
```

To also **delete persistent model/database data** (irreversible; the model will need downloading again):

```sh
docker compose down --volumes
```

Docker images may remain cached after `make down`; they are not running containers. `make dev` reuses them when unchanged and rebuilds when needed. If you quit Docker Desktop, the engine stops; start Docker Desktop and run `make dev` to bring the stack back.

## Run services directly on the host (optional)

Install uv and Node.js 22+, then install dependencies:

```sh
uv sync --all-groups
cd frontend && npm ci
```

For Ollama, install/start Ollama on the host and pull the configured model, then start the backend and frontend in separate terminals:

```sh
ollama serve
ollama pull llama3.2:1b
```

```sh
uv run uvicorn llm_app.main:app --app-dir src --reload --port 8000
```

```sh
cd frontend && npm run dev
```

Open <http://localhost:5173>. Vite proxies API requests to `http://localhost:8000`. For OpenCode Go, export `LLM_BACKEND=opencode-go`, `OPENCODE_GO_API_KEY`, and optionally `OPENCODE_GO_MODEL` in the backend terminal before starting Uvicorn. Host-run processes do not automatically load `.env`.

## API

### App chat

`POST /api/chat`

```json
{
  "messages": [
    { "role": "user", "content": "Explain prompt injection briefly." }
  ]
}
```

The response contains `answer`, configured `model`, `latency_ms`, and token `usage`. Supported message roles are `system`, `user`, and `assistant`. The endpoint returns a complete response; the frontend simulates word-by-word typing afterward.

### OpenAI-compatible chat

- `GET /v1/models`
- `POST /v1/chat/completions` (non-streaming)

Point compatible clients and evaluation tools such as garak at `http://localhost:8000/v1` (or the same origin through the frontend proxy). Only the configured model is served. Streaming requests currently return HTTP 400.

### Health

`GET /health` reports API status, selected backend/model, and whether that model is available. The Compose health check stays healthy while a local model is being pulled or Go credentials are being configured, so the UI can display setup guidance.

## Development checks

```sh
uv sync --all-groups
make check
```

`make check` runs Ruff, pytest, a production frontend build, and Compose configuration validation. CI also runs frontend lint and dependency audit.

## Research question

How much do layered open-source guardrails reduce prompt-injection and jailbreak attack success on an LLM application, and what are the costs in false refusals and latency?

The complete project plan is stored locally at `~/.opencode/plan/idt-llm-cicd.md`.

## Safety and limitations

This is a local development demo, not a production service. The ports are bound to localhost. Do not expose it publicly without authentication, rate limiting, and HTTPS. Model output can be inaccurate; this phase has no guardrails, persistence, or experiment evaluation. Results will depend on the specific model/provider, prompts, hardware, and inference settings.
