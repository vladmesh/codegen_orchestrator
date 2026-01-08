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
# Ubuntu 24.04 comes with ubuntu:ubuntu (1000:1000), rename it to worker
RUN usermod -l worker ubuntu && \
    groupmod -n worker ubuntu && \
    usermod -d /home/worker -m worker
ENV HOME=/home/worker

# Add local bin to PATH
ENV PATH="/home/worker/.local/bin:${PATH}"

# Allow worker to install global npm packages
RUN mkdir -p /usr/local/lib/node_modules && \
    chown -R worker:worker /usr/local/lib/node_modules /usr/local/bin

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

# Install ralph-wiggum plugin for long-running autonomous tasks
# This plugin intercepts Claude's exit attempts and continues iteration
# until completion promise is met or max iterations reached
RUN mkdir -p ~/.claude/plugins && \
    git clone --depth 1 --filter=blob:none --sparse \
        https://github.com/anthropics/claude-code.git /tmp/claude-code && \
    cd /tmp/claude-code && \
    git sparse-checkout set plugins/ralph-wiggum && \
    cp -r plugins/ralph-wiggum ~/.claude/plugins/ && \
    rm -rf /tmp/claude-code

# Default entrypoint
ENTRYPOINT ["/entrypoint.sh"]
