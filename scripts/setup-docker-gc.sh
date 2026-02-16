#!/bin/bash
# Setup BuildKit garbage collection to prevent unbounded cache growth.
# Requires sudo. Run: sudo bash scripts/setup-docker-gc.sh
#
# This configures Docker to auto-clean reclaimable build cache layers
# when total cache exceeds 5GB, keeping the most recently used layers.

set -euo pipefail

DAEMON_JSON="/etc/docker/daemon.json"

if [ ! -f "$DAEMON_JSON" ]; then
    echo '{}' | sudo tee "$DAEMON_JSON" > /dev/null
fi

# Merge gc config into existing daemon.json using python (available on most systems)
python3 -c "
import json, sys
with open('$DAEMON_JSON') as f:
    config = json.load(f)
config.setdefault('builder', {})['gc'] = {
    'enabled': True,
    'defaultKeepStorage': '5gb'
}
print(json.dumps(config, indent=4))
" | sudo tee "$DAEMON_JSON" > /dev/null

echo "Updated $DAEMON_JSON:"
cat "$DAEMON_JSON"

echo ""
echo "Restarting Docker daemon to apply changes..."
sudo systemctl restart docker
echo "Done. BuildKit will now auto-clean cache above 5GB."
