#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../../.." && pwd)"

export PYTHONPATH="${REPO_ROOT}:${PYTHONPATH-}"

cd "${REPO_ROOT}"

if [ -z "${DATABASE_URL-}" ]; then
  echo "[migrate] DATABASE_URL is not set explicitly. Falling back to Settings configuration." >&2
fi

alembic -c services/backend/migrations/alembic.ini upgrade head
