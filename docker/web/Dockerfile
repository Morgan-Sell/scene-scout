# SceneScout web container — FastAPI onboarding and profile UI.
FROM python:3.12-slim-bookworm

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1 \
    WEB_SERVER_PORT=7860

COPY pyproject.toml uv.lock ./
COPY alembic.ini ./
COPY alembic ./alembic
COPY scene_scout ./scene_scout
COPY config ./config

RUN uv sync --frozen --no-dev --extra web

EXPOSE 7860

CMD ["uv", "run", "uvicorn", "scene_scout.web.app:app", "--host", "0.0.0.0", "--port", "7860"]
