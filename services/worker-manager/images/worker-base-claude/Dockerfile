# Worker base image with Claude CLI pre-installed
# Inherits from worker-base-common

FROM worker-base-common:latest

USER root

# Install Node.js for Claude CLI
RUN apt-get update && apt-get install -y --no-install-recommends \
    nodejs \
    npm \
    && rm -rf /var/lib/apt/lists/*

# Install Claude CLI globally
RUN npm install -g @anthropic-ai/claude-code

USER worker
