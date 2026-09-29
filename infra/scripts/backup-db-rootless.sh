#!/usr/bin/env bash
# User-manager backup and independent Docker readback for the owning rootless daemon.
# Installed beside backup-db.sh. Policy contains identity/connectivity, never secrets.
set +x
set -euo pipefail
umask 077

fail() { echo "[backup-rootless] failed operation=$1" >&2; exit 1; }
: "${BACKUP_USER:?BACKUP_USER is required}"
: "${BACKUP_UID:?BACKUP_UID is required}"
: "${BACKUP_RUNTIME_DIR:?BACKUP_RUNTIME_DIR is required}"
[[ "$(id -un)" = "$BACKUP_USER" && "$(id -u)" = "$BACKUP_UID" ]] || fail owning_identity
[[ "$BACKUP_RUNTIME_DIR" = /* && -d "$BACKUP_RUNTIME_DIR" &&
   ! -L "$BACKUP_RUNTIME_DIR" && -O "$BACKUP_RUNTIME_DIR" ]] || fail rootless_runtime
[[ "$(stat -c %a "$BACKUP_RUNTIME_DIR")" = 700 ]] || fail runtime_permissions
socket="$BACKUP_RUNTIME_DIR/docker.sock"
[[ -S "$socket" && ! -L "$socket" && -O "$socket" ]] || fail rootless_socket

# DOCKER_CONTEXT overrides DOCKER_HOST. Clear inherited selectors/TLS settings;
# neither an operator context nor a system-daemon default may redirect this path.
unset DOCKER_CONTEXT DOCKER_TLS_VERIFY DOCKER_CERT_PATH
export DOCKER_HOST="unix://$socket"
case "${1:-}" in
    backup)
        [[ $# = 1 ]] || fail invocation
        exec "${BASH_SOURCE[0]%/*}/backup-db.sh"
        ;;
    docker)
        shift
        [[ $# -gt 0 ]] || fail invocation
        exec docker --host "$DOCKER_HOST" "$@"
        ;;
    *) fail invocation ;;
esac
