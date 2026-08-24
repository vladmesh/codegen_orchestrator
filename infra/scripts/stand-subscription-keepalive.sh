#!/usr/bin/env bash
# Keep the stand's coding-agent sessions refreshable.
#
# Both agents refresh their tokens when they are used, and only then. A stand is
# idle between runs by nature, so the refresh that a live run depends on can sit
# unused for weeks and quietly stop working — which is discovered, without this,
# eight minutes into a mega run.
#
# One trivial call per agent is enough: the point is to exercise the refresh, not
# to do work. Failures are recorded rather than raised, because a keepalive that
# breaks the machine it protects is worse than a stale token; `make
# stand-preflight` is what refuses to start a run.
set -uo pipefail

LOG="${KEEPALIVE_LOG:-$HOME/.stand-keepalive.log}"
CLAUDE_PROFILE="${HOST_CLAUDE_DIR:-$HOME/.claude-worker}"
CODEX_PROFILE="${HOST_CODEX_HOME:-$HOME/.codex-worker}"
PROMPT='reply with the single word: ready'

log() { printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$1" >> "$LOG"; }

# /tmp so neither agent picks up a project's configuration on the way.
cd /tmp || exit 0

if CLAUDE_CONFIG_DIR="$CLAUDE_PROFILE" timeout 180 "$HOME/.local/bin/claude" -p "$PROMPT" > /dev/null 2>&1; then
    log "claude ok"
else
    log "claude FAILED (exit $?) — run: claude auth login"
fi

if CODEX_HOME="$CODEX_PROFILE" timeout 300 codex exec --sandbox danger-full-access \
    --skip-git-repo-check "$PROMPT" > /dev/null 2>&1; then
    log "codex ok"
else
    log "codex FAILED (exit $?) — run: codex login --device-auth"
fi

# Keep the log readable rather than unbounded.
tail -n 200 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
