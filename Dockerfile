FROM ghcr.io/astral-sh/uv:0.12.19 AS uv

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

COPY --from=uv /uv /uvx /bin/
WORKDIR /app

COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --frozen --no-dev

RUN useradd --create-home --uid 10001 appuser
USER appuser

EXPOSE 8000
CMD ["uv", "run", "--no-sync", "uvicorn", "llm_app.main:app", "--app-dir", "src", "--host", "0.0.0.0", "--port", "8000"]
