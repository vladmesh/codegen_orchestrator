# syntax=docker/dockerfile:1.4
# ===================================
# Multi-stage Dockerfile for Scaffolder Service
# ===================================

# === Stage 1: Builder ===
FROM python:3.12.13-slim AS builder

WORKDIR /build

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY services/scaffolder/requirements.lock ./
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --prefix=/install -r requirements.lock

# === Stage 2: Production ===
FROM python:3.12.13-slim AS production

WORKDIR /app

# Install runtime dependencies: git (clone/push), make (make setup), uv (copier + venv)
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    make \
    && rm -rf /var/lib/apt/lists/*

# Tooling is part of the production image contract; pin it like the test image.
RUN pip install --no-cache-dir uv==0.11.29 \
    && uv tool install copier==9.17.1

COPY --from=builder /install /usr/local

# Copy shared as plain files
COPY shared ./shared
ENV PYTHONPATH=/app

COPY services/scaffolder/src ./src

CMD ["python", "-m", "src.main"]

# Which tree this image baked shared from; read by scripts/shared_freshness.py.
ARG SOURCE_HASH=unknown
LABEL org.codegen.worker_source_hash=$SOURCE_HASH
