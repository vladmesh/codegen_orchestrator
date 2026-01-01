# syntax=docker/dockerfile:1.4
# ===================================
# Universal Worker Base Image
# Runs any CLI agent with orchestrator-cli pre-installed
# ===================================

FROM ubuntu:24.04

# Avoid interactive prompts during apt install
ENV DEBIAN_FRONTEND=noninteractive

# Install base system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    # Python
    python3 \
    python3-pip \
    python3-venv \
    # Node.js (for Claude Code, Gemini CLI, etc.)
    nodejs \
    npm \
    # Essential tools
    git \
    curl \
    ca-certificates \
    jq \
    && rm -rf /var/lib/apt/lists/*

# Create non-root user
RUN useradd -m -s /bin/bash worker
ENV HOME=/home/worker

# Add local bin to PATH
ENV PATH="/home/worker/.local/bin:${PATH}"

# Copy and install orchestrator-cli
COPY shared/cli /opt/orchestrator-cli
RUN pip3 install --no-cache-dir --break-system-packages /opt/orchestrator-cli

# Copy entrypoint
COPY services/universal-worker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# Setup workspace
WORKDIR /workspace

# Switch to non-root user
USER worker

# Default entrypoint
ENTRYPOINT ["/entrypoint.sh"]
