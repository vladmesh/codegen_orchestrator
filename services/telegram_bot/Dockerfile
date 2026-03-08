# syntax=docker/dockerfile:1.4
# ===================================
# Multi-stage Dockerfile for Telegram Bot Service
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
COPY services/telegram_bot/requirements.lock ./
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --prefix=/install -r requirements.lock

# === Stage 2: Production ===
# Create minimal runtime image
FROM python:3.12-slim AS production

WORKDIR /app

# Copy installed packages from builder
COPY --from=builder /install /usr/local

# Copy shared as plain files (deps are in service requirements.lock)
COPY shared ./shared
ENV PYTHONPATH=/app

# Copy source code
COPY services/telegram_bot/src ./src

# Run application
CMD ["python", "-m", "src.main"]
