# Worker Common Base Image
# Contains shared dependencies, Python environment, and packages common to all workers.

# -----------------
# Builder Stage
# -----------------
FROM python:3.12-slim AS builder

WORKDIR /build

# Install build dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    gcc \
    libc6-dev \
    && rm -rf /var/lib/apt/lists/*

# Copy packages
COPY shared /build/shared
COPY packages/worker-wrapper /build/packages/worker-wrapper
COPY packages/orchestrator-cli /build/packages/orchestrator-cli

# Build wheels for all packages
RUN pip wheel --no-cache-dir --wheel-dir=/build/wheels /build/shared
RUN pip wheel --no-cache-dir --wheel-dir=/build/wheels /build/packages/worker-wrapper
RUN pip wheel --no-cache-dir --wheel-dir=/build/wheels --find-links=/build/wheels /build/packages/orchestrator-cli

# -----------------
# Runtime Stage
# -----------------
FROM python:3.12-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1
# Prevent user-site packages (~/.local/lib/python3.12/site-packages) from shadowing
# system packages. Without this, `pip install --user shared` by the agent would shadow
# the wrapper's `shared` package, causing ModuleNotFoundError on wrapper restart.
# System site-packages (/usr/local/lib/...) are already protected by root ownership.
# Agents should use virtual environments for project dependencies.
ENV PYTHONNOUSERSITE=1

# Install runtime dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    curl \
    jq \
    make \
    && rm -rf /var/lib/apt/lists/*

# Tooling packages for native EXEC_MODE (stable layer — cached unless versions change).
# Placed BEFORE volatile code-dependent wheels so code changes don't invalidate this layer.
RUN pip install --no-cache-dir \
    ruff==0.14.5 \
    copier==9.4.1 \
    xenon==0.9.1 \
    pytest==8.2.0 \
    pytest-cov==4.1.0 \
    mypy==1.10.0 \
    types-PyYAML \
    PyYAML \
    jinja2

# Copy wheels from builder (volatile — changes on shared/packages code edits)
COPY --from=builder /build/wheels /wheels

# Install Python packages from wheels
RUN pip install --no-cache-dir /wheels/*.whl && rm -rf /wheels

# Create non-root user
RUN useradd -m -s /bin/bash worker

# Setup workspace
RUN mkdir /workspace && chown worker:worker /workspace
WORKDIR /workspace

USER worker

# Healthcheck
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD worker-wrapper healthcheck || exit 1

# Entrypoint
ENTRYPOINT ["worker-wrapper"]
