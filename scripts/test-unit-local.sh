#!/usr/bin/env bash
# Run all unit tests locally without Docker.
# Requires: uv sync (once)
#
# Each service uses `from src.xxx` imports, so we set PYTHONPATH per service.
# Packages (worker-wrapper) and shared use proper package
# names and don't need PYTHONPATH overrides.
#
# We clear env vars that leak from the root .env to avoid pydantic-settings
# picking up extra/conflicting values in service Settings classes.
#
# Usage:
#   ./scripts/test-unit-local.sh           # parallel (default, fast)
#   ./scripts/test-unit-local.sh --serial  # sequential (verbose output)

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
MODE="${1:-parallel}"

# Minimal env for unit tests — no real services needed.
# Services with pydantic-settings will validate these at import time.
CLEAN_ENV=(
    env -i
    HOME="$HOME"
    PATH="$PATH"
    VIRTUAL_ENV="${VIRTUAL_ENV:-}"
    PYTHONPATH=""
    # Dummy values for services that validate env at import
    REDIS_URL="redis://localhost:6379/0"
    API_BASE_URL="http://localhost:8000"
    OPENAI_API_KEY="sk-test-not-real"
    LANGSMITH_API_KEY="ls-test-not-real"
    GITHUB_APP_ID="12345"
    GITHUB_APP_PRIVATE_KEY_PATH="/dev/null"
    TELEGRAM_BOT_TOKEN="0000000000:test-token"
    SECRETS_ENCRYPTION_KEY="wHhIQWmPfLt60oHdxzbQhY1ZKnUon12e5_SuZ33xDxc="
    ORCHESTRATOR_HOSTNAME="localhost"
    REGISTRY_USER="test"
    REGISTRY_PASSWORD="test"
    WORKER_MANAGER_URL="http://localhost:8001"
    WORKER_REDIS_URL="redis://localhost:6379/0"
    WORKER_API_URL="http://localhost:8000"
    LK_DOMAIN="https://lk.test.example.com"
    INTERNAL_API_KEY="test-internal-key"
    LK_JWT_SECRET="test-lk-jwt-secret"
    DATABASE_URL="postgresql+asyncpg://test:test@localhost:5432/test"
)

# --- Serial mode (original behavior, verbose) ---

run_tests_serial() {
    local label="$1"
    local test_dir="$2"
    local pythonpath="${3:-}"

    if [ ! -d "$ROOT/$test_dir" ] || [ -z "$(ls -A "$ROOT/$test_dir" 2>/dev/null)" ]; then
        echo "⏭  $label — no tests found"
        return
    fi

    echo "🧪 $label..."
    local workdir="${pythonpath:-$ROOT}"
    if (cd "$workdir" && "${CLEAN_ENV[@]}" \
       PYTHONPATH="${pythonpath:+$pythonpath:}" \
       python -m pytest "$ROOT/$test_dir" -v --tb=short -q) 2>&1; then
        PASSED+=("$label")
    else
        FAILED+=("$label")
    fi
    echo ""
}

# --- Parallel mode (fast, logs to tmpfiles) ---

LOGDIR=""
run_tests_parallel() {
    local label="$1"
    local test_dir="$2"
    local pythonpath="${3:-}"

    if [ ! -d "$ROOT/$test_dir" ] || [ -z "$(ls -A "$ROOT/$test_dir" 2>/dev/null)" ]; then
        echo 0 > "$LOGDIR/$label.rc"
        return
    fi

    local workdir="${pythonpath:-$ROOT}"
    local rc=0
    (cd "$workdir" && "${CLEAN_ENV[@]}" \
       PYTHONPATH="${pythonpath:+$pythonpath:}" \
       python -m pytest "$ROOT/$test_dir" --tb=short -q) \
       > "$LOGDIR/$label.log" 2>&1 || rc=$?
    echo "$rc" > "$LOGDIR/$label.rc"
}

# --- Shared test list ---

ALL_SUITES=(
    "api|services/api/tests/unit|$ROOT/services/api"
    "langgraph|services/langgraph/tests/unit|$ROOT/services/langgraph"
    "telegram_bot|services/telegram_bot/tests/unit|$ROOT/services/telegram_bot"
    "scheduler|services/scheduler/tests/unit|$ROOT/services/scheduler"
    "worker-manager|services/worker-manager/tests/unit|$ROOT/services/worker-manager"
    "infra-service|services/infra-service/tests/unit|$ROOT/services/infra-service"
    "worker-wrapper|packages/worker-wrapper/tests/unit|"
    "shared|shared/tests|"
)

FAILED=()
PASSED=()

if [ "$MODE" = "--serial" ]; then
    for suite in "${ALL_SUITES[@]}"; do
        IFS='|' read -r label test_dir pythonpath <<< "$suite"
        run_tests_serial "$label" "$test_dir" "$pythonpath"
    done
else
    LOGDIR=$(mktemp -d)
    trap 'rm -rf "$LOGDIR"' EXIT

    for suite in "${ALL_SUITES[@]}"; do
        IFS='|' read -r label test_dir pythonpath <<< "$suite"
        run_tests_parallel "$label" "$test_dir" "$pythonpath" &
    done
    wait

    for suite in "${ALL_SUITES[@]}"; do
        IFS='|' read -r label _ _ <<< "$suite"
        rc=$(cat "$LOGDIR/$label.rc" 2>/dev/null || echo 1)
        if [ "$rc" = "0" ]; then
            PASSED+=("$label")
        else
            FAILED+=("$label")
            echo "❌ $label"
            cat "$LOGDIR/$label.log" 2>/dev/null || echo "(no log)"
            echo ""
        fi
    done
fi

# Summary
echo "========================================="
echo "Passed: ${#PASSED[@]}"
echo "Failed: ${#FAILED[@]}"
if [ ${#FAILED[@]} -gt 0 ]; then
    echo ""
    echo "FAILED:"
    for f in "${FAILED[@]}"; do
        echo "  - $f"
    done
    exit 1
fi
echo "All unit tests passed!"
