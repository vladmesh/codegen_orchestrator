# syntax=docker/dockerfile:1.4
# ===================================
# Workers Spawner Service
# Unified CLI agent container management
# ===================================

FROM python:3.12-slim

# Install system dependencies (for Docker CLI)
RUN apt-get update && apt-get install -y --no-install-recommends \
    docker.io \
    && rm -rf /var/lib/apt/lists/*

# Setup work directory
WORKDIR /app

# Copy shared modules
COPY shared /app/shared

# Copy service code
COPY services/workers-spawner/pyproject.toml /app/
COPY services/workers-spawner/src /app/src

# Install dependencies
RUN pip install --no-cache-dir -e .

# Default command
CMD ["python", "-m", "workers_spawner.main"]
