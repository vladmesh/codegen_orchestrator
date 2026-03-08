# syntax=docker/dockerfile:1.4
# ===================================
# Multi-stage Dockerfile for API Service
# ===================================

# === Stage 1: Builder ===
# Install dependencies in a separate stage for caching
FROM python:3.12-slim AS builder

WORKDIR /build

# Install system dependencies needed for building Python packages
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Copy and install service dependencies from lock file first (changes rarely)
COPY services/api/requirements.lock ./
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --prefix=/install -r requirements.lock

# Install shared dependencies first (changes rarely — only when pyproject.toml changes)
COPY shared/pyproject.toml /tmp/shared-pyproject.toml
RUN --mount=type=cache,target=/root/.cache/pip \
    python -c "import tomllib; deps=tomllib.load(open('/tmp/shared-pyproject.toml','rb'))['project']['dependencies']; print('\n'.join(deps))" \
    > /tmp/shared-requirements.txt && \
    pip install --prefix=/install -r /tmp/shared-requirements.txt

# Install shared package code (no deps to download — fast)
COPY shared ./shared
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --prefix=/install --no-deps ./shared

# === Stage 2: Production ===
# Create minimal runtime image
FROM python:3.12-slim AS production

WORKDIR /app

# Copy installed packages from builder
COPY --from=builder /install /usr/local

# Install curl for healthcheck
RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

# Copy source code
COPY services/api/src ./src

COPY services/api/migrations ./migrations
COPY services/api/alembic.ini ./alembic.ini
COPY services/api/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# Expose port
EXPOSE 8000

# Run application (migrations first)
CMD ["/entrypoint.sh"]
