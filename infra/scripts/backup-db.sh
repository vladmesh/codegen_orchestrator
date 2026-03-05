#!/usr/bin/env bash
# Backup orchestrator PostgreSQL database via docker compose exec.
# Produces gzipped SQL dump, retains last N daily backups.
#
# Optional env vars:
#   BACKUP_DIR     — where to store backups (default: /opt/backups/orchestrator)
#   BACKUP_RETAIN  — number of backups to keep (default: 7)
#   COMPOSE_DIR    — path to docker-compose project (default: /opt/codegen_orchestrator)

set -euo pipefail

BACKUP_DIR="${BACKUP_DIR:-/opt/backups/orchestrator}"
BACKUP_RETAIN="${BACKUP_RETAIN:-7}"
COMPOSE_DIR="${COMPOSE_DIR:-/opt/codegen_orchestrator}"

TIMESTAMP=$(date +%Y-%m-%d_%H-%M)
FILENAME="orchestrator_${TIMESTAMP}.sql.gz"
FILEPATH="${BACKUP_DIR}/${FILENAME}"

mkdir -p "${BACKUP_DIR}"

echo "[backup] Starting database backup..."
docker compose -C "${COMPOSE_DIR}" exec -T db \
    pg_dump -U "${POSTGRES_USER:-postgres}" "${POSTGRES_DB:-orchestrator}" \
    | gzip > "${FILEPATH}"

SIZE=$(du -h "${FILEPATH}" | cut -f1)
echo "[backup] Created ${FILEPATH} (${SIZE})"

# Rotate: keep only the most recent N backups
BACKUPS_TO_DELETE=$(ls -1t "${BACKUP_DIR}"/orchestrator_*.sql.gz 2>/dev/null | tail -n +$((BACKUP_RETAIN + 1)))
if [ -n "${BACKUPS_TO_DELETE}" ]; then
    echo "${BACKUPS_TO_DELETE}" | xargs rm -f
    DELETED=$(echo "${BACKUPS_TO_DELETE}" | wc -l)
    echo "[backup] Rotated: removed ${DELETED} old backup(s)"
fi

echo "[backup] Done. Backups in ${BACKUP_DIR}:"
ls -lh "${BACKUP_DIR}"/orchestrator_*.sql.gz 2>/dev/null | tail -5
