# syntax=docker/dockerfile:1.4
# ===================================
# Agent Worker Container
# Runs Claude Code CLI in isolated environment
# ===================================

FROM node:20-slim

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    ca-certificates \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install Claude Code CLI
RUN npm install -g @anthropic-ai/claude-code

# Setup workspace
WORKDIR /workspace
ENV HOME=/home/node

# Disable telemetry and auto-updates
ENV DISABLE_TELEMETRY=1
ENV DISABLE_ERROR_REPORTING=1
ENV DISABLE_AUTOUPDATER=1

# Switch to non-root user (node user already exists in node image)
USER node

# Default entrypoint - Claude CLI
ENTRYPOINT ["claude"]

