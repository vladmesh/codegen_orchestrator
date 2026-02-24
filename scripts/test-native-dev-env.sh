#!/bin/bash
# =============================================================================
# Test: Native Dev Environment (Phase 3.5 Re-verification)
# =============================================================================
# Tests the full flow:
# 1. Create worker via Redis Streams
# 2. Verify dual-network connectivity
# 3. Verify env vars (ORCHESTRATOR_WORKER_MANAGER_URL, etc.)
# 4. Run copier to scaffold a project
# 5. Test EXEC_MODE=native make format/lint
# 6. Test orchestrator dev-env start-infra db
# =============================================================================

set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

WORKER_ID="test-native-$$"
CONTAINER_NAME="worker-${WORKER_ID}"
SERVICE_TEMPLATE_PATH="/home/vlad/projects/service-template"
WORKSPACE_HOST_PATH="/tmp/codegen/workspaces/${WORKER_ID}/workspace"

pass() { echo -e "${GREEN}[PASS]${NC} $1"; }
fail() { echo -e "${RED}[FAIL]${NC} $1"; FAILURES=$((FAILURES + 1)); }
info() { echo -e "${YELLOW}[INFO]${NC} $1"; }

FAILURES=0

cleanup() {
    info "Cleaning up worker ${WORKER_ID}..."
    # Delete worker via Redis
    redis-cli -p 6379 XADD worker:commands '*' data \
        "{\"command\":\"delete\",\"request_id\":\"cleanup-${WORKER_ID}\",\"worker_id\":\"${WORKER_ID}\"}" \
        > /dev/null 2>&1 || true
    sleep 3
    # Force cleanup if Redis delete didn't work
    docker rm -f "${CONTAINER_NAME}" 2>/dev/null || true
    docker network rm "dev_proj_${WORKER_ID}" 2>/dev/null || true
    sudo rm -rf "/tmp/codegen/workspaces/${WORKER_ID}" 2>/dev/null || true
}

trap cleanup EXIT

# =============================================================================
echo ""
echo "=========================================="
echo " Step 1: Create Worker via Redis Streams"
echo "=========================================="
# =============================================================================

COMMAND_JSON="{\"command\":\"create\",\"request_id\":\"test-${WORKER_ID}\",\"config\":{\"name\":\"${WORKER_ID}\",\"worker_type\":\"developer\",\"agent_type\":\"claude\",\"instructions\":\"Test native dev environment worker\",\"allowed_commands\":[],\"capabilities\":[\"git\",\"github_cli\"]}}"

info "Sending CreateWorkerCommand for ${WORKER_ID}..."
redis-cli -p 6379 XADD worker:commands '*' data "${COMMAND_JSON}"

info "Waiting for container to start (up to 60s)..."
for i in $(seq 1 30); do
    if docker ps --format '{{.Names}}' | grep -q "^${CONTAINER_NAME}$"; then
        pass "Container ${CONTAINER_NAME} is running"
        break
    fi
    if [ "$i" -eq 30 ]; then
        fail "Container ${CONTAINER_NAME} did not start within 60s"
        # Check response stream for errors
        info "Checking worker:responses:developer for errors..."
        redis-cli -p 6379 XREVRANGE worker:responses:developer + - COUNT 5
        exit 1
    fi
    sleep 2
done

# Small delay for container init
sleep 2

# =============================================================================
echo ""
echo "=========================================="
echo " Step 2: Verify Dual-Network Setup"
echo "=========================================="
# =============================================================================

# Check container networks
NETWORKS=$(docker inspect "${CONTAINER_NAME}" --format '{{range $k, $v := .NetworkSettings.Networks}}{{$k}} {{end}}')
info "Container networks: ${NETWORKS}"

if echo "${NETWORKS}" | grep -q "codegen_internal"; then
    pass "Connected to codegen_internal (primary)"
else
    fail "NOT connected to codegen_internal"
fi

if echo "${NETWORKS}" | grep -q "dev_proj_${WORKER_ID}"; then
    pass "Connected to dev_proj_${WORKER_ID} (dev network)"
else
    fail "NOT connected to dev_proj_${WORKER_ID}"
fi

# =============================================================================
echo ""
echo "=========================================="
echo " Step 3: Verify Environment Variables"
echo "=========================================="
# =============================================================================

check_env() {
    local var_name="$1"
    local expected="$2"
    local actual
    actual=$(docker exec "${CONTAINER_NAME}" printenv "${var_name}" 2>/dev/null || echo "NOT_SET")
    if [ "${actual}" = "${expected}" ]; then
        pass "${var_name}=${actual}"
    elif [ "${actual}" = "NOT_SET" ]; then
        fail "${var_name} is NOT SET (expected: ${expected})"
    else
        fail "${var_name}=${actual} (expected: ${expected})"
    fi
}

check_env "WORKER_ID" "${WORKER_ID}"
check_env "ORCHESTRATOR_WORKER_MANAGER_URL" "http://worker-manager:8000"
check_env "ORCHESTRATOR_API_URL" "http://api:8000"
check_env "ORCHESTRATOR_REDIS_URL" "redis://redis:6379"

# =============================================================================
echo ""
echo "=========================================="
echo " Step 4: Test Network Connectivity"
echo "=========================================="
# =============================================================================

# Test: worker → worker-manager
WM_HEALTH=$(docker exec "${CONTAINER_NAME}" curl -sf http://worker-manager:8000/health 2>&1 || echo "FAILED")
if echo "${WM_HEALTH}" | grep -qi "ok\|healthy\|status"; then
    pass "worker → worker-manager:8000/health OK"
else
    fail "worker → worker-manager:8000/health FAILED: ${WM_HEALTH}"
fi

# Test: worker → api
API_HEALTH=$(docker exec "${CONTAINER_NAME}" curl -sf http://api:8000/health 2>&1 || echo "FAILED")
if echo "${API_HEALTH}" | grep -qi "ok\|healthy\|status"; then
    pass "worker → api:8000/health OK"
else
    fail "worker → api:8000/health FAILED: ${API_HEALTH}"
fi

# Test: worker → redis
REDIS_PING=$(docker exec "${CONTAINER_NAME}" bash -c 'echo PING | nc -w 2 redis 6379' 2>&1 || echo "FAILED")
if echo "${REDIS_PING}" | grep -q "PONG"; then
    pass "worker → redis:6379 PONG"
else
    info "worker → redis:6379 (nc not available or failed, skipping)"
fi

# =============================================================================
echo ""
echo "=========================================="
echo " Step 5: Verify Workspace Bind-Mount"
echo "=========================================="
# =============================================================================

# Create a file from inside the container
docker exec "${CONTAINER_NAME}" bash -c 'echo "hello from container" > /workspace/.test-bindmount'
if [ -f "${WORKSPACE_HOST_PATH}/.test-bindmount" ]; then
    pass "Workspace bind-mount works (container → host)"
    rm -f "${WORKSPACE_HOST_PATH}/.test-bindmount"
else
    fail "Workspace bind-mount NOT working"
fi

# =============================================================================
echo ""
echo "=========================================="
echo " Step 6: Scaffold Project with Copier"
echo "=========================================="
# =============================================================================

# Install copier + uv inside the worker
info "Installing copier and uv in worker..."
docker exec "${CONTAINER_NAME}" bash -c '
    pip install copier 2>&1 | tail -1
    curl -LsSf https://astral.sh/uv/install.sh 2>/dev/null | sh 2>&1 | tail -1
    echo "export PATH=\"\$HOME/.local/bin:\$PATH\"" >> ~/.bashrc
' 2>&1 | while read line; do info "  $line"; done

# Run copier to scaffold project
info "Running copier to scaffold test-native project..."
COPIER_OUTPUT=$(docker exec "${CONTAINER_NAME}" bash -c "
    export PATH=\"\$HOME/.local/bin:\$PATH\"
    export HOST_UID=\$(id -u)
    export HOST_GID=\$(id -g)
    cd /workspace
    copier copy ${SERVICE_TEMPLATE_PATH} . \
        --vcs-ref=HEAD \
        --defaults \
        -d project_name=test-native \
        -d modules=backend \
        2>&1
" 2>&1)
COPIER_EXIT=$?

echo "${COPIER_OUTPUT}" | tail -20

if [ ${COPIER_EXIT} -eq 0 ]; then
    pass "Copier scaffold succeeded"
else
    fail "Copier scaffold failed (exit ${COPIER_EXIT})"
    info "Full output above"
fi

# Check key files exist
for f in Makefile pyproject.toml shared/spec/models.yaml; do
    if docker exec "${CONTAINER_NAME}" test -f "/workspace/${f}"; then
        pass "Generated: ${f}"
    else
        fail "Missing: ${f}"
    fi
done

# Check file ownership (should NOT be root)
OWNER=$(docker exec "${CONTAINER_NAME}" stat -c '%U' /workspace/Makefile 2>/dev/null || echo "unknown")
if [ "${OWNER}" = "root" ]; then
    fail "Makefile is root-owned (permission issue)"
else
    pass "Makefile owned by: ${OWNER}"
fi

# =============================================================================
echo ""
echo "=========================================="
echo " Step 7: Test EXEC_MODE=native (format/lint)"
echo "=========================================="
# =============================================================================

# Install project deps with uv
info "Installing project dependencies with uv..."
docker exec "${CONTAINER_NAME}" bash -c '
    export PATH="$HOME/.local/bin:$PATH"
    cd /workspace
    uv sync 2>&1 | tail -5
' 2>&1 | while read line; do info "  $line"; done

# Test: make format in native mode
info "Running EXEC_MODE=native make format..."
FORMAT_OUTPUT=$(docker exec "${CONTAINER_NAME}" bash -c '
    export PATH="$HOME/.local/bin:$PATH"
    cd /workspace
    EXEC_MODE=native make format 2>&1
' 2>&1)
FORMAT_EXIT=$?
echo "${FORMAT_OUTPUT}" | tail -10

if [ ${FORMAT_EXIT} -eq 0 ]; then
    pass "EXEC_MODE=native make format succeeded"
else
    fail "EXEC_MODE=native make format failed (exit ${FORMAT_EXIT})"
fi

# Test: make lint in native mode
info "Running EXEC_MODE=native make lint..."
LINT_OUTPUT=$(docker exec "${CONTAINER_NAME}" bash -c '
    export PATH="$HOME/.local/bin:$PATH"
    cd /workspace
    EXEC_MODE=native make lint 2>&1
' 2>&1)
LINT_EXIT=$?
echo "${LINT_OUTPUT}" | tail -10

if [ ${LINT_EXIT} -eq 0 ]; then
    pass "EXEC_MODE=native make lint succeeded"
else
    fail "EXEC_MODE=native make lint failed (exit ${LINT_EXIT})"
fi

# =============================================================================
echo ""
echo "=========================================="
echo " Step 8: Test orchestrator dev-env CLI"
echo "=========================================="
# =============================================================================

# Check if orchestrator CLI is available
if docker exec "${CONTAINER_NAME}" which orchestrator > /dev/null 2>&1; then
    pass "orchestrator CLI is available"
else
    fail "orchestrator CLI not found in PATH"
    # Try installing it
    info "Attempting to install orchestrator-cli..."
    docker exec "${CONTAINER_NAME}" bash -c '
        export PATH="$HOME/.local/bin:$PATH"
        pip install /app/packages/orchestrator-cli 2>&1 | tail -3
    ' 2>&1 | while read line; do info "  $line"; done
fi

# Test start-infra --help
HELP_OUTPUT=$(docker exec "${CONTAINER_NAME}" bash -c '
    export PATH="$HOME/.local/bin:$PATH"
    orchestrator dev-env start-infra --help 2>&1
' 2>&1)
if echo "${HELP_OUTPUT}" | grep -q "services"; then
    pass "orchestrator dev-env start-infra --help works"
else
    fail "orchestrator dev-env start-infra --help failed"
    echo "${HELP_OUTPUT}"
fi

# Test: start-infra db (the actual compose proxy test)
info "Running: orchestrator dev-env start-infra db..."

# First, we need a docker-compose file in the workspace for the db service
# Check if one exists from copier scaffold
if docker exec "${CONTAINER_NAME}" test -f /workspace/infra/compose.base.yml; then
    pass "compose.base.yml exists from scaffold"

    INFRA_OUTPUT=$(docker exec "${CONTAINER_NAME}" bash -c '
        export PATH="$HOME/.local/bin:$PATH"
        orchestrator dev-env compose -- -f infra/compose.base.yml up -d --wait db 2>&1
    ' 2>&1)
    INFRA_EXIT=$?
    echo "${INFRA_OUTPUT}" | tail -15

    if [ ${INFRA_EXIT} -eq 0 ]; then
        pass "orchestrator dev-env compose (db) succeeded"

        # Verify db container is running in the dev network
        DB_CONTAINERS=$(docker ps --filter "label=com.docker.compose.project=worker_${WORKER_ID}" --format '{{.Names}} {{.Status}}')
        if [ -n "${DB_CONTAINERS}" ]; then
            pass "Sidecar DB container running: ${DB_CONTAINERS}"
        else
            info "No sidecar containers found (may use different labeling)"
        fi
    else
        fail "orchestrator dev-env compose (db) failed (exit ${INFRA_EXIT})"
    fi
else
    info "No compose.base.yml found, testing with minimal compose file..."
    docker exec "${CONTAINER_NAME}" bash -c '
        mkdir -p /workspace/infra
        cat > /workspace/infra/docker-compose.test.yml << EOF
services:
  db:
    image: postgres:16-alpine
    environment:
      POSTGRES_DB: test_db
      POSTGRES_USER: test
      POSTGRES_PASSWORD: test
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U test"]
      interval: 2s
      timeout: 5s
      retries: 10
EOF
    '

    INFRA_OUTPUT=$(docker exec "${CONTAINER_NAME}" bash -c '
        export PATH="$HOME/.local/bin:$PATH"
        orchestrator dev-env compose -- -f infra/docker-compose.test.yml up -d --wait db 2>&1
    ' 2>&1)
    INFRA_EXIT=$?
    echo "${INFRA_OUTPUT}" | tail -15

    if [ ${INFRA_EXIT} -eq 0 ]; then
        pass "orchestrator dev-env compose (test db) succeeded"
    else
        fail "orchestrator dev-env compose (test db) failed (exit ${INFRA_EXIT})"
    fi
fi

# =============================================================================
echo ""
echo "=========================================="
echo " RESULTS"
echo "=========================================="
# =============================================================================

if [ ${FAILURES} -eq 0 ]; then
    echo -e "${GREEN}All tests passed!${NC}"
else
    echo -e "${RED}${FAILURES} test(s) failed.${NC}"
fi

exit ${FAILURES}
