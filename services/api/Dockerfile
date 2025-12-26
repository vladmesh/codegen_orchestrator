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
RUN pip install --no-cache-dir --prefix=/install ./shared

# Copy and install service dependencies
COPY services/api/pyproject.toml ./
RUN pip install --no-cache-dir --prefix=/install .

# === Stage 2: Production ===
# Create minimal runtime image
FROM python:3.12-slim AS production

WORKDIR /app

# Copy installed packages from builder
COPY --from=builder /install /usr/local

# Copy source code
COPY services/api/src ./src

COPY alembic ./alembic
COPY alembic.ini ./

# Expose port
EXPOSE 8000

# Run application
CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8000"]
