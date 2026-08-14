# SceneScout pipeline container — runs the orchestrator via UAT CLI (dry-run).
FROM python:3.12-slim-bookworm

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1 \
    DRY_RUN=true

COPY pyproject.toml uv.lock ./
COPY alembic.ini ./
COPY alembic ./alembic
COPY scene_scout ./scene_scout
COPY config ./config

RUN uv sync --frozen --no-dev

COPY README.md ./

CMD ["uv", "run", "python", "-m", "scene_scout.cli", "uat", "--prompt", "Docker pipeline stub run", "--dry-run"]
