#!/usr/bin/env bash
# Run a remote script on the deploy host, retrying only a failed SSH connection.
#
# The deploy's file-only steps (write .env and secret files, check the written .env,
# check out the revision, read back the release record) are idempotent, so a transient
# SSH timeout should cost a retry, not the deploy. What must never be retried is a
# script that ran and failed on its merits: that is a real refusal.
#
# ssh reports its own failure as exit 255 and otherwise passes the remote status
# through, so 255 is the only status retried, at most MAX_ATTEMPTS times. The remote
# wrapper reports a script's own exit 255 as 1, so a script can never be mistaken for
# a dropped connection and re-run.
#
# The remote script is read from stdin, not taken as an argument, so what it carries
# (the .env, a private key) never appears in a process list on either side. It is
# buffered in a private temp file so every attempt receives all of it. Each attempt's
# stdout is buffered too and only the last attempt's is emitted, so a caller that
# redirects stdout never gets a partial first attempt followed by a full second one.
#
# Usage: bash infra/scripts/deploy-ssh.sh <<'REMOTE'
#          ...remote script...
#        REMOTE
#
# Required env vars:
#   SSH_PRIVATE_KEY   the deploy key
#   PROD_HOST         the deploy host
#   DEPLOY_SSH_USER   the account on it

set -euo pipefail

: "${SSH_PRIVATE_KEY:?SSH_PRIVATE_KEY is required}"
: "${PROD_HOST:?PROD_HOST is required}"
: "${DEPLOY_SSH_USER:?DEPLOY_SSH_USER is required}"

MAX_ATTEMPTS=3
SSH_CONNECTION_FAILURE=255
BACKOFF_SECONDS=10

# The remote status of the script, with 255 kept for ssh's own failures.
REMOTE_COMMAND='bash -s; status=$?; if [ "$status" -eq 255 ]; then echo "remote script exited 255; reported as 1" >&2; exit 1; fi; exit "$status"'

workdir="$(mktemp -d)"
trap 'rm -rf "${workdir}"' EXIT
chmod 700 "${workdir}"
umask 077
key="${workdir}/key"
script="${workdir}/script"
output="${workdir}/output"
printf '%s\n' "${SSH_PRIVATE_KEY}" > "${key}"
cat > "${script}"

attempt=1
while :; do
    if ssh -i "${key}" \
        -o BatchMode=yes \
        -o IdentitiesOnly=yes \
        -o StrictHostKeyChecking=accept-new \
        -o ConnectTimeout=20 \
        -o ServerAliveInterval=15 \
        -o ServerAliveCountMax=4 \
        "${DEPLOY_SSH_USER}@${PROD_HOST}" "${REMOTE_COMMAND}" \
        < "${script}" > "${output}"; then
        status=0
    else
        status=$?
    fi
    if [ "${status}" -ne "${SSH_CONNECTION_FAILURE}" ] || [ "${attempt}" -ge "${MAX_ATTEMPTS}" ]; then
        break
    fi
    delay=$((BACKOFF_SECONDS * attempt))
    echo "SSH connection to ${PROD_HOST} failed (attempt ${attempt}/${MAX_ATTEMPTS}); retrying in ${delay}s" >&2
    sleep "${delay}"
    attempt=$((attempt + 1))
done

cat "${output}"
if [ "${status}" -eq "${SSH_CONNECTION_FAILURE}" ]; then
    echo "FATAL: SSH connection to ${PROD_HOST} failed ${MAX_ATTEMPTS} times; giving up" >&2
fi
exit "${status}"
