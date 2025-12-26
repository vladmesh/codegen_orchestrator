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

# Copy and install service dependencies
COPY services/scheduler/pyproject.toml ./
RUN pip install --no-cache-dir --prefix=/install .

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

COPY services/api/src/clients ./src/clients
COPY shared ./shared

# Run application
CMD ["python", "-m", "src.main"]
