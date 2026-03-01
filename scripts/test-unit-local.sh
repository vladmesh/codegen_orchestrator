#!/usr/bin/env bash
# Run all unit tests locally without Docker.
# Requires: uv sync (once)
#
# Each service uses `from src.xxx` imports, so we set PYTHONPATH per service.
# Packages (orchestrator-cli, worker-wrapper) and shared use proper package
# names and don't need PYTHONPATH overrides.
#
# We clear env vars that leak from the root .env to avoid pydantic-settings
# picking up extra/conflicting values in service Settings classes.

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
FAILED=()
PASSED=()

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
    GITHUB_WEBHOOK_SECRET="test-webhook-secret"
    ORCHESTRATOR_HOSTNAME="localhost"
    REGISTRY_USER="test"
    REGISTRY_PASSWORD="test"
    WORKER_MANAGER_URL="http://localhost:8001"
)

run_tests() {
    local label="$1"
    local test_dir="$2"
    local pythonpath="${3:-}"

    if [ ! -d "$ROOT/$test_dir" ] || [ -z "$(ls -A "$ROOT/$test_dir" 2>/dev/null)" ]; then
        echo "⏭  $label — no tests found"
        return
    fi

    echo "🧪 $label..."
    # Run from service dir to isolate .env file loading (some services have env_file=".env")
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

# Services (need PYTHONPATH for `from src.xxx` imports)
run_tests "api"            "services/api/tests/unit"            "$ROOT/services/api"
run_tests "langgraph"      "services/langgraph/tests/unit"      "$ROOT/services/langgraph"
run_tests "telegram_bot"   "services/telegram_bot/tests/unit"   "$ROOT/services/telegram_bot"
run_tests "scheduler"      "services/scheduler/tests/unit"      "$ROOT/services/scheduler"
run_tests "worker-manager" "services/worker-manager/tests/unit" "$ROOT/services/worker-manager"
run_tests "infra-service"  "services/infra-service/tests/unit"  "$ROOT/services/infra-service"

# Packages (use proper package names, no PYTHONPATH needed)
run_tests "orchestrator-cli" "packages/orchestrator-cli/tests/unit"
run_tests "worker-wrapper"   "packages/worker-wrapper/tests/unit"

# Shared
run_tests "shared" "shared/tests"

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
