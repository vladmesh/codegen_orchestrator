#!/bin/sh
set -e

ADMIN_USER="${ADMIN_USER:-admin}"

if [ -z "$ADMIN_PASSWORD" ]; then
    echo "ERROR: ADMIN_PASSWORD environment variable is required" >&2
    exit 1
fi

# Generate htpasswd file from env vars at container start
htpasswd -cb /etc/nginx/.htpasswd "$ADMIN_USER" "$ADMIN_PASSWORD"

# Generate Langfuse API proxy auth header (Basic Auth from public:secret keys)
if [ -n "$LANGFUSE_PUBLIC_KEY" ] && [ -n "$LANGFUSE_SECRET_KEY" ]; then
    LANGFUSE_AUTH=$(printf '%s:%s' "$LANGFUSE_PUBLIC_KEY" "$LANGFUSE_SECRET_KEY" | base64)
    cat > /etc/nginx/langfuse_auth.conf <<EOF
proxy_set_header Authorization "Basic ${LANGFUSE_AUTH}";
EOF
    echo "Langfuse API proxy auth configured"
else
    # Empty file — proxy will work but Langfuse will reject with 401
    : > /etc/nginx/langfuse_auth.conf
    echo "WARNING: LANGFUSE_PUBLIC_KEY or LANGFUSE_SECRET_KEY not set, Langfuse proxy disabled"
fi

exec nginx -g 'daemon off;'
