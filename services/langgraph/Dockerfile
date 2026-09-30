# syntax=docker/dockerfile:1.4
# ===================================
# Multi-stage Dockerfile for LangGraph Service
# ===================================

# === Stage 1: Builder ===
# Install dependencies in a separate stage for caching
FROM python:3.12.13-slim AS builder

WORKDIR /build

# Install system dependencies needed for building
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    git \
    && rm -rf /var/lib/apt/lists/*

# Copy and install service dependencies from lock file first (changes rarely)
COPY services/langgraph/requirements.lock ./
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --prefix=/install -r requirements.lock

# === Stage 2: Claude Code for the `claude` LLM channel ===
# The same installer and the same pin as the Claude worker image: the version is read
# from that Dockerfile's `ARG CLAUDE_CODE_VERSION`, the one place it is written, so the
# heads and the workers can never run two Claude versions. The installer puts a
# versioned binary under root's home; only that file is carried over, to a path the
# non-root CLI child can execute.
FROM python:3.12.13-slim AS claude-installer

RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

COPY services/worker-manager/images/worker-base-claude/Dockerfile /pins/worker-base-claude.Dockerfile

# A fetch that still fails after curl's retries prints the CI-INFRA-CAUSE line the
# worker image prints (scripts/ci-infra.sh watch).
RUN version="$(sed -n 's/^ARG CLAUDE_CODE_VERSION=//p' /pins/worker-base-claude.Dockerfile)" \
    && test -n "$version" \
    && installer=$(mktemp) \
    && { curl --retry 3 --retry-connrefused -fsSL -o "$installer" https://claude.ai/install.sh \
        || { printf '%s=%s\n' CI-INFRA-CAUSE claude-installer-fetch >&2; exit 1; }; } \
    && test -s "$installer" \
    && bash -s -- "$version" < "$installer" \
    && rm "$installer" \
    && install -D -m 0755 "$(readlink -f /root/.local/bin/claude)" /opt/claude/claude \
    && test "$(/opt/claude/claude --version)" = "$version (Claude Code)"

# === Stage 3: Production ===
# Create minimal runtime image
FROM python:3.12.13-slim AS production

WORKDIR /app

# Install runtime dependencies (git for GitHub operations; nodejs and npm for the
# Codex CLI, installed the way the Codex worker image installs it)
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    openssh-client \
    nodejs \
    npm \
    && rm -rf /var/lib/apt/lists/*

# The Codex CLI for the `codex` LLM channel, at the Codex worker image's pin
# (`ARG CODEX_CLI_VERSION` of that Dockerfile). Heads and workers share one file-backed
# profile, so they must run the same CLI version: a different one may write auth.json
# in a format the other cannot read. The version check runs in a throwaway HOME: the
# CLI writes under its home even for --version.
COPY services/worker-manager/images/worker-base-codex/Dockerfile /tmp/pins/worker-base-codex.Dockerfile
RUN version="$(sed -n 's/^ARG CODEX_CLI_VERSION=//p' /tmp/pins/worker-base-codex.Dockerfile)" \
    && test -n "$version" \
    && npm install --global "@openai/codex@${version}" \
    && npm cache clean --force \
    && home="$(mktemp -d)" \
    && test "$(HOME="$home" codex --version)" = "codex-cli ${version}" \
    && rm -rf "$home" /tmp/pins

COPY --from=claude-installer /opt/claude/claude /usr/local/bin/claude

# Copy installed packages from builder
COPY --from=builder /install /usr/local

# Copy shared as plain files (deps are in service requirements.lock)
COPY shared ./shared
ENV PYTHONPATH=/app

# Copy source code
COPY services/langgraph/src ./src
COPY secrets.yaml ./secrets.yaml

# Every flag the channel adapters pass must be one the installed CLIs accept: the
# build reads their help output and fails naming any flag they do not list.
RUN python -m src.llm.cli_contract

# Run service
CMD ["python", "-m", "src"]

# Which tree this image baked shared from; read by scripts/shared_freshness.py.
ARG SOURCE_HASH=unknown
LABEL org.codegen.worker_source_hash=$SOURCE_HASH
