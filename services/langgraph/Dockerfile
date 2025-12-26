# syntax=docker/dockerfile:1.4
# ===================================
# Multi-stage Dockerfile for LangGraph Service
# ===================================

# === Stage 1: Builder ===
# Install dependencies in a separate stage for caching
FROM python:3.12-slim AS builder

WORKDIR /build

# Install system dependencies needed for building
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    git \
    && rm -rf /var/lib/apt/lists/*

# Copy and install shared module first
COPY shared ./shared
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --no-cache-dir --prefix=/install ./shared

# Copy and install service dependencies from lock file (faster, no resolution)
COPY services/langgraph/requirements.lock ./
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --no-cache-dir --prefix=/install -r requirements.lock

# === Stage 2: Production ===
# Create minimal runtime image with Ansible runtime dependencies
FROM python:3.12-slim AS production

WORKDIR /app

# Install runtime dependencies for Ansible (git, ssh client)
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    openssh-client \
    sshpass \
    && rm -rf /var/lib/apt/lists/*

# Copy installed packages from builder
COPY --from=builder /install /usr/local

# Copy source code and infrastructure
COPY services/langgraph/src ./src
COPY services/infrastructure ./services/infrastructure
COPY secrets.yaml ./secrets.yaml

# Run worker
CMD ["python", "-m", "src.worker"]
