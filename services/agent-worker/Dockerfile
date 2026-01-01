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

# Install Python for orchestrator-cli
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 \
    python3-pip \
    python3-venv \
    && rm -rf /var/lib/apt/lists/*


# Setup workspace
WORKDIR /workspace
ENV HOME=/home/node

# Disable telemetry and auto-updates
ENV DISABLE_TELEMETRY=1
ENV DISABLE_ERROR_REPORTING=1
ENV DISABLE_AUTOUPDATER=1

# Add local bin to PATH
ENV PATH="/home/node/.local/bin:${PATH}"


# Switch to non-root user (node user already exists in node image)
USER node

# Copy CLI code from shared location
COPY --chown=node:node shared/cli /home/node/cli

# Install CLI
RUN pip3 install --no-cache-dir --break-system-packages /home/node/cli

# Copy Skills
COPY --chown=node:node skills /home/node/.claude/skills

# Copy System Prompt
COPY --chown=node:node CLAUDE.md /home/node/.claude/CLAUDE.md


# Default entrypoint - Claude CLI
ENTRYPOINT ["claude"]

