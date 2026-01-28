# syntax=docker/dockerfile:1.4
# ===================================
# Universal Worker Base Image
# Minimal base for all agent types (Claude, Factory, etc.)
# Agent-specific tools are installed at runtime via INSTALL_COMMANDS
# ===================================

FROM ubuntu:24.04

# Avoid interactive prompts during apt install
ENV DEBIAN_FRONTEND=noninteractive

# Install base system dependencies only
RUN apt-get update && apt-get install -y --no-install-recommends \
    # Python (for orchestrator-cli)
    python3 \
    python3-pip \
    # Essential tools
    git \
    curl \
    ca-certificates \
    jq \
    && rm -rf /var/lib/apt/lists/*

# Create non-root user
# Ubuntu 24.04 comes with ubuntu:ubuntu (1000:1000), rename it to worker
RUN usermod -l worker ubuntu && \
    groupmod -n worker ubuntu && \
    usermod -d /home/worker -m worker
ENV HOME=/home/worker

# Add local bin to PATH
ENV PATH="/home/worker/.local/bin:${PATH}"

# Copy shared modules (schemas needed by CLI)
COPY shared/schemas /opt/shared/schemas
RUN echo '"""Shared schemas for CLI."""' > /opt/shared/__init__.py
ENV PYTHONPATH="/opt:${PYTHONPATH}"

# Copy and install orchestrator-cli
COPY shared/cli /opt/orchestrator-cli
RUN pip3 install --no-cache-dir --break-system-packages /opt/orchestrator-cli

# Copy entrypoint
COPY services/universal-worker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# Setup workspace
WORKDIR /workspace
RUN chown worker:worker /workspace

# Switch to non-root user
USER worker

# Default entrypoint
ENTRYPOINT ["/entrypoint.sh"]
