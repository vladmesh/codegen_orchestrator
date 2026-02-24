#!/bin/sh
# Copy host SSH keys into container's /root/.ssh and fix ownership.
# Host ~/.ssh is mounted read-only at /host-ssh (uid 1000).
# SSH refuses keys owned by another user, so we copy them.
if [ -d /host-ssh ]; then
    mkdir -p /root/.ssh
    cp -a /host-ssh/* /root/.ssh/ 2>/dev/null
    chown -R root:root /root/.ssh
    chmod 700 /root/.ssh
    chmod 600 /root/.ssh/id_* 2>/dev/null
    chmod 644 /root/.ssh/*.pub 2>/dev/null
    chmod 600 /root/.ssh/config 2>/dev/null
fi

exec "$@"
