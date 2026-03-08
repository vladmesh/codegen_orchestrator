# Worker base image with Claude CLI pre-installed
# Inherits from worker-base-common
#
# Uses a separate stage to install Claude CLI so that the expensive
# curl | bash download is cached independently of base image changes.

# Global ARG must be before any FROM to be usable in FROM instructions
ARG BASE_IMAGE=worker-base-common:latest

# --- Stage 1: Install Claude CLI (cached unless install.sh changes) ---
FROM python:3.12-slim AS claude-installer

RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

RUN useradd -m -s /bin/bash worker
USER worker

RUN curl -fsSL https://claude.ai/install.sh | bash

# --- Stage 2: Final image ---
FROM ${BASE_IMAGE}

USER root

# Install dependencies for Claude CLI native binary
RUN apt-get update && apt-get install -y --no-install-recommends \
    ripgrep \
    && rm -rf /var/lib/apt/lists/*

USER worker

# Copy pre-installed Claude CLI from cache stage (avoids re-downloading)
COPY --from=claude-installer /home/worker/.local /home/worker/.local

# Add Claude launcher to PATH (installer creates symlink in ~/.local/bin)
ENV PATH="/home/worker/.local/bin:${PATH}"

# Create minimal config for API key mode (prevents errors when no OAuth session exists)
RUN mkdir -p /home/worker/.claude && \
    echo '{}' > /home/worker/.claude/settings.json

# Disable auto-updates and telemetry in containerized environment
ENV DISABLE_AUTOUPDATER=1
ENV DISABLE_TELEMETRY=1
