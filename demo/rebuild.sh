#!/bin/sh
# Rebuild the demo frontend, run its tests and restart the mock service.
set -e
cd "$(dirname "$0")/../services/admin-frontend"
npm test >/tmp/brimos-fe-test.log 2>&1 || { tail -40 /tmp/brimos-fe-test.log; exit 1; }
VITE_DEMO=1 npm run build >/tmp/brimos-fe-build.log 2>&1 || { tail -40 /tmp/brimos-fe-build.log; exit 1; }
XDG_RUNTIME_DIR=/run/user/$(id -u) systemctl --user restart brimos-dashboard-demo.service
sleep 1.5
curl -sf http://127.0.0.1:8790/health >/dev/null && echo "rebuilt, tests ok, service up"
