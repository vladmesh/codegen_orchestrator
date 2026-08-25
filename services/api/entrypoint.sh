#!/bin/sh
set -e

cd /app
alembic upgrade head

if [ "$ENVIRONMENT" = "test" ]; then
    uvicorn src.main:app --host 0.0.0.0 --port 8000 &
    api_pid=$!
    until curl --fail --silent http://localhost:8000/health >/dev/null; do
        sleep 1
    done
    python /app/scripts/seed_system_configs.py \
        --api-base-url http://localhost:8000 \
        --configs-path /app/scripts/system_configs.yaml
    if [ -n "$SYSTEM_CONFIGS_TEST_OVERLAY" ]; then
        python /app/scripts/seed_system_configs.py \
            --api-base-url http://localhost:8000 \
            --configs-path "$SYSTEM_CONFIGS_TEST_OVERLAY"
    fi
    wait "$api_pid"
    exit $?
fi

exec uvicorn src.main:app --host 0.0.0.0 --port 8000
