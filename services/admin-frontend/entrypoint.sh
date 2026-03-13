#!/bin/sh
set -e

ADMIN_USER="${ADMIN_USER:-admin}"

if [ -z "$ADMIN_PASSWORD" ]; then
    echo "ERROR: ADMIN_PASSWORD environment variable is required" >&2
    exit 1
fi

# Generate htpasswd file from env vars at container start
htpasswd -cb /etc/nginx/.htpasswd "$ADMIN_USER" "$ADMIN_PASSWORD"

exec nginx -g 'daemon off;'
