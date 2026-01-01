# syntax=docker/dockerfile:1.4
# ===================================
# Workers Spawner Service
# Unified CLI agent container management
# ===================================

FROM python:3.12-slim

# Install Docker CLI from official Docker repository
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    gnupg \
    && install -m 0755 -d /etc/apt/keyrings \
    && curl -fsSL https://download.docker.com/linux/debian/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg \
    && chmod a+r /etc/apt/keyrings/docker.gpg \
    && echo \
    "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/debian \
    $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | \
    tee /etc/apt/sources.list.d/docker.list > /dev/null \
    && apt-get update && apt-get install -y --no-install-recommends \
    docker-ce-cli \
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
