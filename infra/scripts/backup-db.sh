#!/usr/bin/env bash
# Verified custom archive of the existing Compose database. No host DB credentials.
# Required: COMPOSE_DIR (absolute), COMPOSE_ARGS (whitespace-separated Compose flags),
# BACKUP_DIR (absolute, owned by this user), BACKUP_KIND, BACKUP_CONTOUR.
# Nightly requires BACKUP_RETAIN (1..365); predeploy/maintenance require BACKUP_LABEL.
set +x
set -euo pipefail
umask 077

fail() { echo "[backup] failed operation=$1" >&2; exit 1; }
: "${COMPOSE_DIR:?COMPOSE_DIR is required}"
: "${COMPOSE_ARGS:?COMPOSE_ARGS is required}"
: "${BACKUP_DIR:?BACKUP_DIR is required}"
: "${BACKUP_KIND:?BACKUP_KIND is required}"
: "${BACKUP_CONTOUR:?BACKUP_CONTOUR is required}"
[[ "$COMPOSE_DIR" = /* && -d "$COMPOSE_DIR" ]] || fail compose_directory
[[ "$BACKUP_DIR" = /* ]] || fail backup_directory
case "$BACKUP_CONTOUR" in production|stand) ;; *) fail contour ;; esac
case "$BACKUP_KIND" in
    nightly)
        : "${BACKUP_RETAIN:?BACKUP_RETAIN is required for nightly}"
        [[ "$BACKUP_RETAIN" =~ ^[1-9][0-9]{0,2}$ ]] || fail retention_policy
        (( BACKUP_RETAIN <= 365 )) || fail retention_policy
        prefix=orchestrator_nightly
        ;;
    predeploy|maintenance)
        : "${BACKUP_LABEL:?BACKUP_LABEL is required for protected archives}"
        [[ "$BACKUP_LABEL" =~ ^[a-zA-Z0-9][a-zA-Z0-9._-]{0,159}$ ]] || fail backup_label
        prefix="orchestrator_${BACKUP_KIND}_${BACKUP_LABEL}"
        ;;
    *) fail backup_kind ;;
esac

# Never loosen a secret-bearing directory. Refuse symlinks and another user's path.
mkdir -p "$BACKUP_DIR" 2>/dev/null || fail backup_directory
[[ -d "$BACKUP_DIR" && ! -L "$BACKUP_DIR" && -O "$BACKUP_DIR" ]] || fail backup_directory
chmod 700 "$BACKUP_DIR" 2>/dev/null || fail backup_permissions
# Serialize publication/rotation, including concurrent manual nightly invocations.
exec 9> "$BACKUP_DIR/.backup.lock"
chmod 600 "$BACKUP_DIR/.backup.lock"
flock -x 9 || fail backup_lock

read -r -a compose_args <<< "$COMPOSE_ARGS"
cd "$COMPOSE_DIR"
container=$(docker compose --project-directory "$COMPOSE_DIR" "${compose_args[@]}" \
    ps --all -q db 2>/dev/null) || fail database_resolution
if [ -z "$container" ]; then
    if [ "$BACKUP_CONTOUR" = stand ] && [ "$BACKUP_KIND" = predeploy ]; then
        echo "[backup] empty_stand_bootstrap label=$BACKUP_LABEL"
        exit 0
    fi
    fail database_missing
fi
[[ "$container" =~ ^[0-9a-f]{64}$ ]] || fail database_identity
running=$(docker inspect --format '{{.State.Running}}' "$container" 2>/dev/null) \
    || fail database_inspection
[ "$running" = true ] || fail database_not_running

temporary=$(mktemp "$BACKUP_DIR/.${prefix}_$(date -u +%Y%m%dT%H%M%S%NZ)_XXXXXXXX.dump.partial") \
    || fail temporary_archive
trap 'rm -f -- "$temporary"' EXIT
trap 'exit 1' HUP INT TERM
# Identity and password come only from the resolved running container. Credentials
# stay in its environment, never Docker/pg_dump argv or the SSH script/logs.
docker exec -i "$container" sh -eu -c '
    : "${POSTGRES_USER:?}" "${POSTGRES_DB:?}" "${POSTGRES_PASSWORD:?}"
    export PGUSER="$POSTGRES_USER" PGDATABASE="$POSTGRES_DB" PGPASSWORD="$POSTGRES_PASSWORD"
    exec pg_dump --format=custom
' > "$temporary" 2>/dev/null || fail pg_dump
[ -s "$temporary" ] || fail empty_archive
docker exec -i "$container" pg_restore --list < "$temporary" > /dev/null 2>&1 \
    || fail archive_list
chmod 600 "$temporary" || fail archive_permissions
bytes=$(stat -c %s "$temporary")
filename=${temporary##*/}
final="$BACKUP_DIR/${filename#.}"
final=${final%.partial}
mv -- "$temporary" "$final" || fail archive_publication

if [ "$BACKUP_KIND" = nightly ]; then
    # Only this producer's final nightly names; no partial, legacy SQL, symlink,
    # predeploy or secret-conversion artifact can be selected. Lock is still held.
    python3 -I - "$BACKUP_DIR" "$BACKUP_RETAIN" <<'PY' || fail retention
from pathlib import Path
import re
import sys

directory = Path(sys.argv[1])
pattern = re.compile(r"orchestrator_nightly_[0-9]{8}T[0-9]{15}Z_[a-zA-Z0-9]{8}\.dump")
archives = [p for p in directory.iterdir() if pattern.fullmatch(p.name)
            and not p.is_symlink() and p.is_file()]
archives.sort(key=lambda p: (p.stat().st_mtime_ns, p.name), reverse=True)
for archive in archives[int(sys.argv[2]):]:
    archive.unlink()
PY
fi
echo "[backup] verified path=$final bytes=$bytes archive_list_exit=0 kind=$BACKUP_KIND"
