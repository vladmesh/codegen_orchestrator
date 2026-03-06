#!/usr/bin/env bash
# SSH to a managed server using SSH key from the API.
#
# Usage:
#   ./infra/scripts/ssh-to-server.sh <server_ip> [command...]
#
# If no command given, opens interactive shell.
# Fetches SSH key from API by matching server IP to handle.

set -euo pipefail

API_URL="${API_URL:-http://localhost:8000}"

if [ $# -lt 1 ]; then
    echo "Usage: $0 <server_ip> [command...]"
    exit 1
fi

SERVER_IP="$1"
shift

# Resolve handle from IP
HANDLE=$(curl -sf "${API_URL}/api/servers/" \
    | python3 -c "
import sys, json
servers = json.load(sys.stdin)
for s in servers:
    if s.get('public_ip') == '${SERVER_IP}':
        print(s['handle'])
        break
" 2>/dev/null)

if [ -z "$HANDLE" ]; then
    echo "ERROR: No server found with IP ${SERVER_IP}" >&2
    exit 1
fi

# Fetch SSH key
SSH_KEY=$(curl -sf "${API_URL}/api/servers/${HANDLE}/ssh-key" \
    | python3 -c "import sys,json; print(json.load(sys.stdin)['ssh_key'])" 2>/dev/null)

if [ -z "$SSH_KEY" ]; then
    echo "ERROR: No SSH key stored for server ${HANDLE}" >&2
    exit 1
fi

# Write to tempfile
TMPKEY=$(mktemp /tmp/orch_ssh_XXXXXX)
trap 'rm -f "$TMPKEY"' EXIT
echo "$SSH_KEY" > "$TMPKEY"
chmod 600 "$TMPKEY"

# Clear stale host key
ssh-keygen -f "$HOME/.ssh/known_hosts" -R "$SERVER_IP" 2>/dev/null || true

if [ $# -eq 0 ]; then
    exec ssh -i "$TMPKEY" -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10 "root@${SERVER_IP}"
else
    ssh -i "$TMPKEY" -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10 "root@${SERVER_IP}" "$@"
fi
