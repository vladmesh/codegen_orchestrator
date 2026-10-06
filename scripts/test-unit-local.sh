#!/usr/bin/env bash
# Run all unit tests locally without Docker.
# Requires: uv sync (once)
#
# Each service uses `from src.xxx` imports, so we prepend the service dir to
# PYTHONPATH. The repo root is always on PYTHONPATH too: `shared` is not an
# installed package, so it can only be imported from the tree.
#
# We clear env vars that leak from the root .env to avoid pydantic-settings
# picking up extra/conflicting values in service Settings classes.
#
# Usage:
#   ./scripts/test-unit-local.sh                # every suite at once (CI, make test-unit)
#   ./scripts/test-unit-local.sh --host         # the light host profile (python -m shared)
#   ./scripts/test-unit-local.sh --serial       # sequential (verbose output)
#
# The host profile is what a weak control host runs: at most UNIT_JOBS suites at a
# time (default 2), `-m "not ci_only"` on every suite, and no live-offline suite.
# Tests marked ci_only, docker, ansible, privileged, kit_gate, subprocess or slow run
# in CI only, and every other host-profile test must finish within 0.5 s. CI runs this
# script without --host, so its coverage does not shrink. Budget for the whole
# profile: 300 s or less and 1.5 GB or less peak RSS at 2 jobs (docs/TESTING.md).

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
MODE="parallel"
HOST_PROFILE=0
for arg in "$@"; do
    case "$arg" in
        --host) HOST_PROFILE=1 ;;
        --serial) MODE="--serial" ;;
        *) echo "unknown argument: $arg" >&2; exit 2 ;;
    esac
done
UNIT_JOBS="${UNIT_JOBS:-2}"

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
    # Keep unit tests isolated from a developer's compose stack. Tests that need
    # system config must supply a ConfigStore fake instead of reading a live API.
    API_BASE_URL="http://127.0.0.1:9"
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
    WORKER_BROKER_INTERNAL_TOKEN="test-worker-broker-internal-token"
    WORKER_BROKER_URL="http://localhost:8001"
    LK_DOMAIN="https://lk.test.example.com"
    TELEGRAM_MAX_CONCURRENT_UPDATES="8"
    INTERNAL_API_KEY="test-internal-key"
    LK_JWT_SECRET="test-lk-jwt-secret"
    DEFAULT_AGENT_TYPE="claude"
    DATABASE_URL="postgresql+asyncpg://test:test@localhost:5432/test"
)

# Every unit test is bounded, so a hang fails in minutes with the test's node id,
# its pending asyncio tasks and every thread's stack (scripts/unit_test_timeout.py)
# instead of running into the CI step timeout with no output. The thread method
# works when the event loop itself is stuck. A test that legitimately needs longer
# carries its own `@pytest.mark.timeout`, which takes precedence over this default.
UNIT_TEST_TIMEOUT_SECONDS=90
TIMEOUT_ARGS=(
    -p scripts.unit_test_timeout
    --timeout="$UNIT_TEST_TIMEOUT_SECONDS"
    --timeout-method=thread
)
# The markers plugin makes every sub-marker (docker, ansible, privileged, kit_gate,
# subprocess, slow) imply ci_only, so the host profile deselects the whole family with
# one expression. The budget plugin (scripts/unit_test_budget.py) fails a host-profile
# test that takes longer than UNIT_TEST_BUDGET_SECONDS (setup+call+teardown).
UNIT_TEST_BUDGET_SECONDS=0.5
MARKER_ARGS=(-p scripts.ci_only_markers -p scripts.unit_test_budget)
if [ "$HOST_PROFILE" = "1" ]; then
    MARKER_ARGS+=(-m "not ci_only")
    MARKER_ARGS+=(--unit-test-budget="$UNIT_TEST_BUDGET_SECONDS")
fi

# Per-suite evidence, both optional and both named after the suite label:
#   UNIT_CPU_DIR     <label>.json with the suite's CPU seconds (python -m shared sets it)
#   UNIT_REPORT_DIR  <label>.xml (junit), and <label>.log with --durations=50 (CI uploads it)
UNIT_CPU_DIR="${UNIT_CPU_DIR:-}"
UNIT_REPORT_DIR="${UNIT_REPORT_DIR:-}"
if [ -n "$UNIT_REPORT_DIR" ]; then
    mkdir -p "$UNIT_REPORT_DIR"
fi

# pytest exits 5 when it collected no test. In the host profile that is a suite whose
# every test is in the ci_only family, which is a pass; in CI it stays a failure.
host_rc() {
    local rc="$1"
    if [ "$HOST_PROFILE" = "1" ] && [ "$rc" = "5" ]; then
        rc=0
    fi
    echo "$rc"
}

suite_report_args() {
    local label="$1"
    REPORT_ARGS=()
    if [ -n "$UNIT_CPU_DIR" ]; then
        REPORT_ARGS+=(--suite-cpu-file="$UNIT_CPU_DIR/$label.json")
    fi
    if [ -n "$UNIT_REPORT_DIR" ]; then
        REPORT_ARGS+=(--junitxml="$UNIT_REPORT_DIR/$label.xml" --durations=50)
    fi
}

# --- Serial mode (original behavior, verbose) ---

run_tests_serial() {
    local label="$1"
    local test_dir="$2"
    local pythonpath="${3:-}"
    local extra_pytest_args="${4:-}"
    local extra_args=()
    if [ -n "$extra_pytest_args" ]; then
        read -r -a extra_args <<< "$extra_pytest_args"
    fi

    if [ ! -d "$ROOT/$test_dir" ] || [ -z "$(ls -A "$ROOT/$test_dir" 2>/dev/null)" ]; then
        echo "⏭  $label — no tests found"
        return
    fi

    echo "🧪 $label..."
    local workdir="${pythonpath:-$ROOT}"
    suite_report_args "$label"
    local log="${UNIT_REPORT_DIR:+$UNIT_REPORT_DIR/$label.log}"
    local rc=0
    (cd "$workdir" && "${CLEAN_ENV[@]}" \
       PYTHONPATH="${pythonpath:+$pythonpath:}$ROOT" \
       python -m pytest "$ROOT/$test_dir" -v --tb=short -q "${TIMEOUT_ARGS[@]}" "${MARKER_ARGS[@]}" "${REPORT_ARGS[@]}" "${extra_args[@]}") 2>&1 \
       | tee "${log:-/dev/null}" || rc=$?
    if [ "$(host_rc "$rc")" = "0" ]; then
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
    local extra_pytest_args="${4:-}"
    local extra_args=()
    if [ -n "$extra_pytest_args" ]; then
        read -r -a extra_args <<< "$extra_pytest_args"
    fi

    if [ ! -d "$ROOT/$test_dir" ] || [ -z "$(ls -A "$ROOT/$test_dir" 2>/dev/null)" ]; then
        echo 0 > "$LOGDIR/$label.rc"
        return
    fi

    local workdir="${pythonpath:-$ROOT}"
    local rc=0
    suite_report_args "$label"
    (cd "$workdir" && "${CLEAN_ENV[@]}" \
       PYTHONPATH="${pythonpath:+$pythonpath:}$ROOT" \
       python -m pytest "$ROOT/$test_dir" --tb=short -q "${TIMEOUT_ARGS[@]}" "${MARKER_ARGS[@]}" "${REPORT_ARGS[@]}" "${extra_args[@]}") \
       > "$LOGDIR/$label.log" 2>&1 || rc=$?
    host_rc "$rc" > "$LOGDIR/$label.rc"
    if [ -n "$UNIT_REPORT_DIR" ]; then
        cp "$LOGDIR/$label.log" "$UNIT_REPORT_DIR/$label.log"
    fi
}

# --- Shared test list ---

OFFLINE_LIVE_IGNORE_ARGS="$(awk 'NF && $1 !~ /^#/ {printf "--ignore=%s ", $1}' "$ROOT/scripts/offline_live_ignores.txt")"

# Every entry here is a CI claim on a test directory, and it covers that directory
# recursively: scripts/check-ci-gate.py walks the tree and fails when a file pytest
# would collect is run by no target.
ALL_SUITES=(
    "api|services/api/tests/unit|$ROOT/services/api"
    "langgraph|services/langgraph/tests/unit|$ROOT/services/langgraph"
    "telegram_bot|services/telegram_bot/tests/unit|$ROOT/services/telegram_bot"
    "scheduler|services/scheduler/tests/unit|$ROOT/services/scheduler"
    "worker-manager|services/worker-manager/tests/unit|$ROOT/services/worker-manager"
    "worker-broker|services/worker-broker/tests/unit|$ROOT/services/worker-broker"
    "infra-service|services/infra-service/tests/unit|$ROOT/services/infra-service"
    "scaffolder|services/scaffolder/tests/unit|$ROOT/services/scaffolder"
    # component despite the sweep it runs in: real local git in a tmpdir, a bare repo
    # for the remote, no network — a fraction of a second, so it belongs pre-push.
    "scaffolder-component|services/scaffolder/tests/component|$ROOT/services/scaffolder"
    "worker-wrapper|packages/worker-wrapper/tests/unit|"
    # component and integration despite the name: both run offline in a tmpdir and
    # finish in under a second, so they belong in the pre-push sweep.
    "worker-wrapper-component|packages/worker-wrapper/tests/component|"
    "worker-wrapper-integration|packages/worker-wrapper/tests/integration|"
    "shared|shared/tests|"
    "scripts|scripts/tests|"
    "repo|tests/unit|"
    "live-offline|tests/live||$OFFLINE_LIVE_IGNORE_ARGS"
)
# Suites the host profile leaves to CI as a whole. scripts/host_sweep.py reads this
# list too: the guard test scans every file of every other suite.
HOST_EXCLUDED_SUITES=(live-offline)

SUITES=()
for suite in "${ALL_SUITES[@]}"; do
    label="${suite%%|*}"
    if [ "$HOST_PROFILE" = "1" ] && [[ " ${HOST_EXCLUDED_SUITES[*]} " == *" $label "* ]]; then
        echo "⏭  $label — CI only, not in the host profile"
        continue
    fi
    SUITES+=("$suite")
done

FAILED=()
PASSED=()

if [ "$MODE" = "--serial" ]; then
    for suite in "${SUITES[@]}"; do
        IFS='|' read -r label test_dir pythonpath extra_pytest_args <<< "$suite"
        run_tests_serial "$label" "$test_dir" "$pythonpath" "$extra_pytest_args"
    done
else
    LOGDIR=$(mktemp -d)
    trap 'rm -rf "$LOGDIR"' EXIT

    running=0
    for suite in "${SUITES[@]}"; do
        IFS='|' read -r label test_dir pythonpath extra_pytest_args <<< "$suite"
        run_tests_parallel "$label" "$test_dir" "$pythonpath" "$extra_pytest_args" &
        # The host profile holds at most UNIT_JOBS suites at once; CI starts them all.
        if [ "$HOST_PROFILE" = "1" ]; then
            running=$((running + 1))
            if [ "$running" -ge "$UNIT_JOBS" ]; then
                wait -n || true
                running=$((running - 1))
            fi
        fi
    done
    wait

    for suite in "${SUITES[@]}"; do
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
if [ "$HOST_PROFILE" = "1" ]; then
    echo "Profile: host (UNIT_JOBS=$UNIT_JOBS, -m \"not ci_only\")"
else
    echo "Profile: full"
fi
echo "Wall time: ${SECONDS}s"
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
