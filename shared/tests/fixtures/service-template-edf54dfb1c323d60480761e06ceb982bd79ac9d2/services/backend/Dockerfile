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
COPY services/backend/pyproject.toml services/backend/uv.lock ./services/backend/
COPY .framework ./.framework
RUN --mount=type=cache,target=/root/.cache/uv \
    cd services/backend \
    && if [ "$INSTALL_DEV_DEPS" = "true" ]; then \
    uv sync --frozen --no-install-project \
    && uv pip install --python .venv/bin/python -e ../../.framework; \
    else \
    uv sync --frozen --no-install-project --no-dev; \
    fi
COPY shared/shared ./shared/shared

FROM base AS runtime
COPY --from=deps /app/services/backend/.venv /app/services/backend/.venv
ENV PATH="/app/services/backend/.venv/bin:${PATH}"
COPY services/backend ./services/backend
COPY services/backend/README.md ./README.md
COPY shared ./shared
COPY .framework ./.framework

RUN chown -R 1000:1000 /app
USER 1000

EXPOSE 8000

CMD ["uvicorn", "services.backend.src.main:app", "--host", "0.0.0.0", "--port", "8000"]
