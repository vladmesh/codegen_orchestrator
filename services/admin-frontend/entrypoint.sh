#!/bin/sh
set -e

ADMIN_USER="${ADMIN_USER:-admin}"

if [ -z "$ADMIN_PASSWORD" ]; then
    echo "ERROR: ADMIN_PASSWORD environment variable is required" >&2
    exit 1
fi

if [ -z "$INTERNAL_API_KEY" ]; then
    echo "ERROR: INTERNAL_API_KEY environment variable is required" >&2
    exit 1
fi

# Generate htpasswd file from env vars at container start
htpasswd -cb /etc/nginx/.htpasswd "$ADMIN_USER" "$ADMIN_PASSWORD"

# Stamp the internal key into the API proxy. nginx cannot read the environment in
# its config, and the key must not be baked into the image; the rendered config
# never leaves the container, and the basic auth above decides who reaches it.
sed -i "s|__INTERNAL_API_KEY__|${INTERNAL_API_KEY}|" /etc/nginx/conf.d/default.conf

exec nginx -g 'daemon off;'
