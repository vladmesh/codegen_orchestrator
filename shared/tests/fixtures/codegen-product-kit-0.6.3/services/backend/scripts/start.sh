#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../../.." && pwd)"

export PYTHONPATH="${REPO_ROOT}:${PYTHONPATH-}"

"${SCRIPT_DIR}/migrate.sh"

exec uvicorn services.backend.src.main:app --host 0.0.0.0 --port "${PORT:-8000}"
