# Worker base image with Claude CLI pre-installed
# Inherits from worker-base-common

ARG BASE_IMAGE=worker-base-common:latest
FROM ${BASE_IMAGE}

USER root

# Install dependencies for Claude CLI native binary
RUN apt-get update && apt-get install -y --no-install-recommends \
    ripgrep \
    && rm -rf /var/lib/apt/lists/*

USER worker

# Install Claude Code via native installer (npm version is deprecated)
# The installer puts the binary in ~/.local/share/claude/versions/X.X.X
# and creates a symlink at ~/.local/bin/claude
RUN curl -fsSL https://claude.ai/install.sh | bash

# Add Claude launcher to PATH (installer creates symlink in ~/.local/bin)
ENV PATH="/home/worker/.local/bin:${PATH}"

# Create minimal config for API key mode (prevents errors when no OAuth session exists)
RUN mkdir -p /home/worker/.claude && \
    echo '{}' > /home/worker/.claude/settings.json

# Disable auto-updates and telemetry in containerized environment
ENV DISABLE_AUTOUPDATER=1
ENV DISABLE_TELEMETRY=1
