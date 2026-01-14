# Worker Common Base Image
# Contains shared dependencies, Python environment, and packages common to all workers.

# -----------------
# Builder Stage
# -----------------
FROM python:3.12-slim as builder

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

# Install runtime dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    curl \
    jq \
    && rm -rf /var/lib/apt/lists/*

# Copy wheels from builder
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
