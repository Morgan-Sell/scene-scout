# SceneScout web container — placeholder Gradio UI.
FROM python:3.12-slim-bookworm

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1 \
    GRADIO_SERVER_PORT=7860

COPY pyproject.toml uv.lock ./
COPY scene_scout ./scene_scout
COPY config ./config

RUN uv sync --frozen --no-dev --extra web

EXPOSE 7860

CMD ["uv", "run", "python", "-m", "scene_scout.gradio_app"]
