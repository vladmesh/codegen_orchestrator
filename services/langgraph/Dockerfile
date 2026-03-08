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

# Copy and install service dependencies from lock file first (changes rarely)
COPY services/langgraph/requirements.lock ./
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

# Install runtime dependencies (git for GitHub operations)
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    openssh-client \
    && rm -rf /var/lib/apt/lists/*

# Copy installed packages from builder
COPY --from=builder /install /usr/local

# Copy source code
COPY services/langgraph/src ./src
COPY secrets.yaml ./secrets.yaml

# Run worker
CMD ["python", "-m", "src.worker"]
