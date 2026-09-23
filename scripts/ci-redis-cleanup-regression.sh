#!/usr/bin/env bash
# The Redis capability cleanup regression of fast-checks: start a throwaway Redis, wait
# for it to answer, and run tests/live/test_capability_cleanup_redis.py against it. The
# workflow runs this under scripts/ci-infra.sh bound, so a docker run or exec that hangs
# is stopped inside the job and named.

# No pipefail, as in the workflow step this came from: grep -q may close the pipe first.
set -eu

container=redis-cleanup-contract
docker run --detach --rm --name "$container" redis:7.4.10-alpine
trap 'docker rm --force "$container"' EXIT
for _ in {1..10}; do
    docker exec "$container" redis-cli --raw ping | grep -qx PONG && break
    sleep 1
done
docker exec "$container" redis-cli --raw ping | grep -qx PONG
LIVE_REDIS_CONTAINER=$container uv run pytest -q tests/live/test_capability_cleanup_redis.py
