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

# Copy and install shared module first
COPY shared ./shared
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --no-cache-dir --prefix=/install ./shared

# Copy and install service dependencies from lock file (faster, no resolution)
COPY services/api/requirements.lock ./
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --no-cache-dir --prefix=/install -r requirements.lock

# === Stage 2: Production ===
# Create minimal runtime image
FROM python:3.12-slim AS production

WORKDIR /app

# Copy installed packages from builder
COPY --from=builder /install /usr/local

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
