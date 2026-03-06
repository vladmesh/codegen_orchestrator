#!/usr/bin/env bash
# Dump server handles + encrypted SSH keys from the API before DB wipe.
# Output: secrets/server_keys.json (array of {handle, public_ip, ssh_key, status, is_managed, labels})
#
# Usage: ./infra/scripts/dump-server-keys.sh [API_URL]
#   API_URL defaults to http://localhost:8000

set -euo pipefail

API_URL="${1:-http://localhost:8000}"
DUMP_FILE="secrets/server_keys.json"

# Check API is reachable
if ! curl -sf "${API_URL}/health" > /dev/null 2>&1; then
    echo "  API not reachable at ${API_URL}, skipping server key dump"
    exit 0
fi

# Fetch server list
servers_json=$(curl -sf "${API_URL}/api/servers/")
server_count=$(echo "$servers_json" | python3 -c "import sys,json; print(len(json.load(sys.stdin)))")

if [ "$server_count" = "0" ]; then
    echo "  No servers in DB, skipping dump"
    exit 0
fi

mkdir -p secrets

# Build dump: for each server, fetch SSH key (if any) and merge
python3 -c "
import json, sys, urllib.request, urllib.error

api = '${API_URL}'
servers = json.loads('''$(echo "$servers_json")''')

dump = []
for srv in servers:
    handle = srv['handle']
    entry = {
        'handle': handle,
        'public_ip': srv.get('public_ip'),
        'status': srv.get('status'),
        'is_managed': srv.get('is_managed', True),
        'labels': srv.get('labels', {}),
        'host': srv.get('host'),
        'ssh_user': srv.get('ssh_user', 'root'),
    }
    # Fetch SSH key
    try:
        req = urllib.request.urlopen(f'{api}/api/servers/{handle}/ssh-key')
        key_data = json.loads(req.read())
        entry['ssh_key'] = key_data.get('ssh_key')
    except urllib.error.HTTPError:
        entry['ssh_key'] = None

    dump.append(entry)

with open('${DUMP_FILE}', 'w') as f:
    json.dump(dump, f, indent=2)

keys_count = sum(1 for e in dump if e.get('ssh_key'))
print(f'  Dumped {len(dump)} servers ({keys_count} with SSH keys) to ${DUMP_FILE}')
"
