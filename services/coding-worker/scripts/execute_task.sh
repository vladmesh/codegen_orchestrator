#!/bin/bash
set -e

echo "=== Coding Worker Starting ==="
echo "Repository: ${REPO}"
echo "Model: ${MODEL}"

# Validate required environment variables
: "${GITHUB_TOKEN:?GITHUB_TOKEN is required}"
: "${FACTORY_API_KEY:?FACTORY_API_KEY is required}"
: "${REPO:?REPO is required}"
: "${TASK_CONTENT:?TASK_CONTENT is required}"
: "${TASK_TITLE:?TASK_TITLE is required}"
: "${MODEL:?MODEL is required}"
: "${ORCHESTRATOR_REDIS_URL:?ORCHESTRATOR_REDIS_URL is required}"
: "${ORCHESTRATOR_REQUEST_ID:?ORCHESTRATOR_REQUEST_ID is required}"
: "${ORCHESTRATOR_EVENTS_CHANNEL:?ORCHESTRATOR_EVENTS_CHANNEL is required}"

# Start Docker daemon (requires sysbox runtime)
echo "=== Starting Docker daemon ==="
dockerd > /var/log/dockerd.log 2>&1 &
DOCKERD_PID=$!

# Wait for Docker to be ready
for i in {1..30}; do
    if docker info > /dev/null 2>&1; then
        echo "Docker daemon is ready"
        break
    fi
    if [ $i -eq 30 ]; then
        echo "ERROR: Docker daemon failed to start"
        cat /var/log/dockerd.log
        exit 1
    fi
    sleep 1
done

WORKER_TYPE="droid"
export WORKER_TYPE

EVENT_SCRIPT="/scripts/publish_event.py"

emit_event() {
    local event_type="$1"
    local payload="$2"
    WORKER_EVENT_EXTRA="$payload" python3 "$EVENT_SCRIPT" "$event_type"
}

emit_progress() {
    local stage="$1"
    local message="$2"
    local pct="$3"

    local payload
    payload=$(STAGE="$stage" MESSAGE="$message" PCT="$pct" python3 - <<'PY'
import json
import os

pct_raw = os.environ.get("PCT")
payload = {
    "stage": os.environ.get("STAGE"),
    "message": os.environ.get("MESSAGE", ""),
    "progress_pct": int(pct_raw) if pct_raw else None,
}
print(json.dumps(payload))
PY
)
    emit_event "progress" "$payload"
}

emit_failed() {
    local error_type="$1"
    local error_message="$2"
    local logs_tail="$3"

    local payload
    payload=$(ERROR_TYPE="$error_type" ERROR_MESSAGE="$error_message" LOGS_TAIL="$logs_tail" python3 - <<'PY'
import json
import os

payload = {
    "error_type": os.environ["ERROR_TYPE"],
    "error_message": os.environ["ERROR_MESSAGE"],
    "logs_tail": os.environ.get("LOGS_TAIL", ""),
}
print(json.dumps(payload))
PY
)
    emit_event "failed" "$payload"
}

handle_error() {
    local exit_code=$?
    trap - ERR
    set +e

    local logs_tail=""
    if [ -f /tmp/droid_output.txt ]; then
        logs_tail=$(tail -n 200 /tmp/droid_output.txt)
    fi

    emit_failed "crash" "Command failed: ${BASH_COMMAND}" "$logs_tail"
    exit "$exit_code"
}

trap handle_error ERR

emit_started_payload=$(python3 - <<'PY'
import json
import os

payload = {
    "repo": os.environ["REPO"],
    "task_summary": os.environ["TASK_TITLE"],
}
print(json.dumps(payload))
PY
)
emit_event "started" "$emit_started_payload"
emit_progress "prepare" "Preparing workspace" 5

# Configure git
git config --global user.email "factory-bot@codegen.ai"
git config --global user.name "Factory Bot"

# Clone repository
echo "=== Cloning repository ==="
emit_progress "clone" "Cloning repository" 15
git clone "https://x-access-token:${GITHUB_TOKEN}@github.com/${REPO}.git" .

# Write context files
echo "=== Writing context files ==="
if [ -n "${AGENTS_CONTENT}" ]; then
    echo "${AGENTS_CONTENT}" > AGENTS.md
    echo "AGENTS.md written"
fi

echo "${TASK_CONTENT}" > TASK.md
echo "TASK.md written"

# Show task
echo "=== Task Content ==="
cat TASK.md

# Run Factory Droid
echo "=== Running Factory Droid ==="
emit_progress "run" "Running coding agent" 40
set +e
droid exec -f TASK.md \
    --skip-permissions-unsafe \
    -m "${MODEL}" \
    2>&1 | tee /tmp/droid_output.txt

DROID_EXIT_CODE=${PIPESTATUS[0]}
set -e

if [ "${DROID_EXIT_CODE}" -ne 0 ]; then
    logs_tail=$(tail -n 200 /tmp/droid_output.txt 2>/dev/null || true)
    set +e
    emit_failed "task_failed" "Droid exited with code ${DROID_EXIT_CODE}" "$logs_tail"
    set -e
    exit "${DROID_EXIT_CODE}"
fi

# Check for changes
echo "=== Checking for changes ==="
emit_progress "commit" "Preparing commit" 70
CHANGED_FILES=""
COMMIT_SHA=""
SUMMARY="No changes to commit"

if git status --porcelain | grep -q .; then
    echo "Changes detected, committing..."
    git add -A
    CHANGED_FILES=$(git diff --name-only --cached)
    git commit -m "feat: ${TASK_TITLE}"

    echo "=== Pushing changes ==="
    emit_progress "push" "Pushing changes" 85
    git push

    COMMIT_SHA=$(git rev-parse HEAD)
    SUMMARY="Committed and pushed changes"
    echo "Pushed commit: ${COMMIT_SHA}"
else
    emit_progress "finalize" "No changes to commit" 90
    echo "No changes to commit"
fi

BRANCH=$(git rev-parse --abbrev-ref HEAD)
emit_completed_payload=$(CHANGED_FILES="${CHANGED_FILES}" COMMIT_SHA="${COMMIT_SHA}" \
    BRANCH="${BRANCH}" SUMMARY="${SUMMARY}" python3 - <<'PY'
import json
import os

files_raw = os.environ.get("CHANGED_FILES", "")
files = [line for line in files_raw.splitlines() if line]

payload = {
    "commit_sha": os.environ.get("COMMIT_SHA") or None,
    "branch": os.environ["BRANCH"],
    "files_changed": files,
    "summary": os.environ["SUMMARY"],
}
print(json.dumps(payload))
PY
)
emit_event "completed" "$emit_completed_payload"

echo "=== Coding Worker Complete ==="
echo "Exit code: ${DROID_EXIT_CODE}"

exit ${DROID_EXIT_CODE}
