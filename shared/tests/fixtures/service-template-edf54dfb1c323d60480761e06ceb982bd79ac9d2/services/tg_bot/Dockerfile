# syntax=docker/dockerfile:1.7
ARG INSTALL_DEV_DEPS="false"

FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app

WORKDIR /app

COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/uv

FROM base AS deps
ARG INSTALL_DEV_DEPS
COPY shared/pyproject.toml ./shared/pyproject.toml
RUN mkdir -p ./shared/shared && touch ./shared/shared/__init__.py
COPY services/tg_bot/pyproject.toml services/tg_bot/uv.lock ./services/tg_bot/
RUN --mount=type=cache,target=/root/.cache/uv \
    cd services/tg_bot \
    && if [ "$INSTALL_DEV_DEPS" = "true" ]; then \
    uv sync --frozen --no-install-project; \
    else \
    uv sync --frozen --no-install-project --no-dev; \
    fi
COPY shared/shared ./shared/shared

FROM base AS runtime
COPY --from=deps /app/services/tg_bot/.venv /app/services/tg_bot/.venv
ENV PATH="/app/services/tg_bot/.venv/bin:${PATH}"
COPY services/tg_bot ./services/tg_bot
COPY services/tg_bot/README.md ./README.md
COPY shared ./shared

RUN chown -R 1000:1000 /app
USER 1000

CMD ["python", "services/tg_bot/src/main.py"]
