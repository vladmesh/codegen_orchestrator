# syntax=docker/dockerfile:1.4
# ===================================
# Multi-stage Dockerfile for Scheduler Service
# ===================================

# === Stage 1: Builder ===
# Install dependencies in a separate stage for caching
FROM python:3.12-slim AS builder

WORKDIR /build

# Install system dependencies needed for building Python packages
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Copy and install service dependencies from lock file (faster, no resolution)
COPY services/scheduler/requirements.lock ./
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --prefix=/install -r requirements.lock

# Install shared dependencies first (cached when pyproject.toml is stable)
COPY shared/pyproject.toml /tmp/shared-pyproject.toml
RUN --mount=type=cache,target=/root/.cache/pip \
    python -c "import tomllib; deps=tomllib.load(open('/tmp/shared-pyproject.toml','rb'))['project']['dependencies']; print('\n'.join(deps))" \
    > /tmp/shared-requirements.txt && \
    pip install --prefix=/install -r /tmp/shared-requirements.txt

# Install shared package code (no deps — fast)
COPY shared ./shared
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --prefix=/install --no-deps ./shared

# === Stage 2: Production ===
# Create minimal runtime image
FROM python:3.12-slim AS production

WORKDIR /app

# Install runtime dependencies (openssh-client for health checks)
RUN apt-get update && apt-get install -y --no-install-recommends \
    openssh-client \
    && rm -rf /var/lib/apt/lists/*

# Copy installed packages from builder
COPY --from=builder /install /usr/local

# Copy source code
COPY services/scheduler/src ./src

# Run application
CMD ["python", "-m", "src.main"]
