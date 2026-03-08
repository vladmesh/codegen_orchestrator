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

# Install dependency wheels first (cached when pyproject.toml files are stable)
COPY shared/pyproject.toml /build/shared/pyproject.toml
COPY packages/worker-wrapper/pyproject.toml /build/packages/worker-wrapper/pyproject.toml
COPY packages/orchestrator-cli/pyproject.toml /build/packages/orchestrator-cli/pyproject.toml
RUN python -c "\
import tomllib, itertools; \
files=['/build/shared/pyproject.toml','/build/packages/worker-wrapper/pyproject.toml','/build/packages/orchestrator-cli/pyproject.toml']; \
deps=set(itertools.chain.from_iterable(tomllib.load(open(f,'rb'))['project']['dependencies'] for f in files)); \
deps = {d for d in deps if not d.strip().lower().startswith('shared')}; \
print('\n'.join(sorted(deps)))" > /tmp/all-deps.txt && \
    pip wheel --no-cache-dir --wheel-dir=/build/wheels -r /tmp/all-deps.txt

# Copy package source and build code-only wheels (fast — deps already wheeled)
COPY shared /build/shared
COPY packages/worker-wrapper /build/packages/worker-wrapper
COPY packages/orchestrator-cli /build/packages/orchestrator-cli
RUN pip wheel --no-cache-dir --no-deps --wheel-dir=/build/wheels /build/shared
RUN pip wheel --no-cache-dir --no-deps --wheel-dir=/build/wheels /build/packages/worker-wrapper
RUN pip wheel --no-cache-dir --no-deps --wheel-dir=/build/wheels /build/packages/orchestrator-cli

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

# Install uv for fast dependency management (copier, ruff etc. installed on-demand via uv tool)
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

# Copy wheels from builder (volatile — changes on shared/packages code edits)
COPY --from=builder /build/wheels /wheels

# Install Python packages from wheels
RUN pip install --no-cache-dir /wheels/*.whl && rm -rf /wheels

# Create non-root user
RUN useradd -m -s /bin/bash worker

# Setup workspace and uv cache directory
RUN mkdir /workspace && chown worker:worker /workspace
RUN mkdir -p /home/worker/.cache/uv && chown -R worker:worker /home/worker/.cache
WORKDIR /workspace

ARG SOURCE_HASH=unknown
LABEL org.codegen.worker_source_hash=$SOURCE_HASH

USER worker

# Healthcheck
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD worker-wrapper healthcheck || exit 1

# Entrypoint
ENTRYPOINT ["worker-wrapper"]
