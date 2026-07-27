# Worker base image with a pinned OpenAI Codex CLI.
# Inherits the shared wrapper, workspace, and non-root worker user.

ARG BASE_IMAGE=worker-base-common:latest
FROM ${BASE_IMAGE}

ARG CODEX_CLI_VERSION=0.144.6

USER root

RUN apt-get update && apt-get install -y --no-install-recommends \
    nodejs \
    npm \
    ripgrep \
    && npm install --global "@openai/codex@${CODEX_CLI_VERSION}" \
    && npm cache clean --force \
    && rm -rf /var/lib/apt/lists/*

RUN mkdir -p /home/worker/.codex && chown worker:worker /home/worker/.codex

ENV CODEX_HOME=/home/worker/.codex

# Own label, not inherited from the base: derived images must carry the hash they were built with
ARG SOURCE_HASH=unknown
LABEL org.codegen.worker_source_hash=$SOURCE_HASH

USER worker
