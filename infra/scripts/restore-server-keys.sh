#!/usr/bin/env bash
# Restore servers from secrets/server_keys.json dump into the API.
# Skips servers that already exist. Used by `make seed`.
#
# Usage: ./infra/scripts/restore-server-keys.sh [API_URL]
#   API_URL defaults to http://localhost:8000

set -euo pipefail

API_URL="${1:-http://localhost:8000}"
DUMP_FILE="secrets/server_keys.json"

# /api/servers/ carries private keys, so it answers nobody. The key is in .env:
# `set -a; . .env; set +a`, or run this through make, which exports it.
: "${INTERNAL_API_KEY:?INTERNAL_API_KEY must be set to reach the API}"

if [ ! -f "$DUMP_FILE" ]; then
    echo "  No server dump found ($DUMP_FILE), skipping"
    exit 0
fi

API_URL="$API_URL" DUMP_FILE="$DUMP_FILE" python3 -c "
import json, os, urllib.request, urllib.error

api = os.environ['API_URL']
internal_headers = {'X-Internal-Key': os.environ['INTERNAL_API_KEY']}
dump_file = os.environ['DUMP_FILE']
servers = json.load(open(dump_file))
restored = 0

for srv in servers:
    handle = srv['handle']
    try:
        urllib.request.urlopen(
            urllib.request.Request(f'{api}/api/servers/{handle}', headers=internal_headers)
        )
        continue
    except urllib.error.HTTPError:
        pass

    payload = json.dumps({
        'handle': handle,
        'host': srv.get('host', ''),
        'public_ip': srv.get('public_ip', ''),
        'ssh_user': srv.get('ssh_user', 'root'),
        'ssh_key': srv.get('ssh_key'),
        'is_managed': srv.get('is_managed', True),
        'status': srv.get('status', 'active'),
        'labels': srv.get('labels', {}),
    }).encode()

    req = urllib.request.Request(
        f'{api}/api/servers/',
        data=payload,
        headers={'Content-Type': 'application/json', **internal_headers},
        method='POST',
    )
    urllib.request.urlopen(req)
    restored += 1

print(f'  Restored {restored}/{len(servers)} servers from {dump_file}')
"
