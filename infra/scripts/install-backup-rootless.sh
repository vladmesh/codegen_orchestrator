#!/usr/bin/env bash
set -euo pipefail

if [ "$#" -ne 5 ]; then
  echo "usage: $0 RELEASE_ROOT POLICY_DIR POLICY_PATH UNIT_DIR BACKUP_DIR" >&2
  exit 2
fi

release_root=$1
policy_dir=$2
policy=$3
unit_dir=$4
backup_dir=$5

install -d -m 0700 "$policy_dir" "$unit_dir" "$backup_dir"
for backup_unit in orchestrator-backup.service orchestrator-backup.timer; do
  install -m 0644 "$release_root/infra/systemd/$backup_unit" "$unit_dir/$backup_unit.next"
  mv -Tf "$unit_dir/$backup_unit.next" "$unit_dir/$backup_unit"
done
if ! test -f "$policy"; then
  install -m 0600 "$release_root/infra/systemd/orchestrator-backup.env.example" "$policy"
fi
chmod 0600 "$policy"
