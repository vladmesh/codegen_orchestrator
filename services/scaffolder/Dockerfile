# syntax=docker/dockerfile:1.4
# ===================================
# Multi-stage Dockerfile for Scaffolder Service
# ===================================

# === Stage 1: Builder ===
FROM python:3.12-slim AS builder

WORKDIR /build

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY services/scaffolder/requirements.lock ./
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --prefix=/install -r requirements.lock

# === Stage 2: Production ===
FROM python:3.12-slim AS production

WORKDIR /app

# Install runtime dependencies: git (clone/push), make (make setup), uv (copier + venv)
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    make \
    curl \
    && rm -rf /var/lib/apt/lists/* \
    && curl -LsSf https://astral.sh/uv/install.sh | sh

ENV PATH="/root/.local/bin:$PATH"

# Install copier via uv tool (cached in image layer)
RUN uv tool install copier

COPY --from=builder /install /usr/local

# Copy shared as plain files
COPY shared ./shared
ENV PYTHONPATH=/app

COPY services/scaffolder/src ./src

CMD ["python", "-m", "src.main"]
