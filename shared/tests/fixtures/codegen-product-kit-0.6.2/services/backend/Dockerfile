# syntax=docker/dockerfile:1.7
FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app

WORKDIR /app

COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/uv

FROM base AS runtime-deps
COPY shared/pyproject.toml ./shared/pyproject.toml
RUN mkdir -p ./shared/shared && touch ./shared/shared/__init__.py
COPY services/backend/packages ./services/backend/packages
COPY services/backend/pyproject.toml services/backend/uv.lock ./services/backend/
RUN --mount=type=cache,target=/root/.cache/uv \
    cd services/backend \
    && uv sync --frozen --no-install-project --no-dev
COPY shared/shared ./shared/shared
COPY codegen_kit ./codegen_kit

FROM runtime-deps AS dev-deps
RUN apt-get update \
    && apt-get install --yes --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project \
    && cd services/backend \
    && uv sync --frozen --no-install-project \
    && .venv/bin/python -c "from pathlib import Path; import site; Path(site.getsitepackages()[0], 'codegen-kit-tooling.pth').write_text('/app/.venv/lib/python3.12/site-packages\\n')"

FROM base AS dev
COPY --from=dev-deps /app/.venv /app/.venv
COPY --from=dev-deps /app/services/backend/.venv /app/services/backend/.venv
ENV PATH="/app/services/backend/.venv/bin:${PATH}"
COPY services/backend ./services/backend
COPY services/backend/README.md ./README.md
COPY shared ./shared
COPY codegen_kit ./codegen_kit

RUN chown -R 1000:1000 /app
USER 1000

EXPOSE 8000

CMD ["uvicorn", "services.backend.src.main:app", "--host", "0.0.0.0", "--port", "8000"]

FROM base AS runtime
COPY --from=runtime-deps /app/services/backend/.venv /app/services/backend/.venv
ENV PATH="/app/services/backend/.venv/bin:${PATH}"
COPY services/backend ./services/backend
COPY services/backend/README.md ./README.md
COPY shared ./shared
COPY codegen_kit ./codegen_kit

RUN chown -R 1000:1000 /app
USER 1000

EXPOSE 8000

CMD ["uvicorn", "services.backend.src.main:app", "--host", "0.0.0.0", "--port", "8000"]
