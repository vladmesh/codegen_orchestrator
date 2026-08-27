#!/bin/sh
set -e

cd /app
alembic upgrade head

if [ "$ENVIRONMENT" = "test" ] && [ "$SEED_SYSTEM_CONFIGS_ON_START" = "true" ]; then
    uvicorn src.main:app --host 0.0.0.0 --port 8000 &
    api_pid=$!
    until curl --fail --silent http://localhost:8000/health >/dev/null; do
        sleep 1
    done
    # The test overlay supplies the initial high paid-work ceilings. Seed it
    # before production defaults so both calls remain initialize-only for
    # protected controls while production-only ordinary configs are still set.
    if [ -n "$SYSTEM_CONFIGS_TEST_OVERLAY" ]; then
        python /app/scripts/seed_system_configs.py \
            --api-base-url http://localhost:8000 \
            --configs-path "$SYSTEM_CONFIGS_TEST_OVERLAY"
    fi
    if [ -n "$SYSTEM_CONFIGS_TEST_OVERLAY" ]; then
        python /app/scripts/seed_system_configs.py \
            --api-base-url http://localhost:8000 \
            --configs-path /app/scripts/system_configs.yaml \
            --skip-key work_admission.max_projects_per_user
    else
        python /app/scripts/seed_system_configs.py \
            --api-base-url http://localhost:8000 \
            --configs-path /app/scripts/system_configs.yaml
    fi
    wait "$api_pid"
    exit $?
fi

exec uvicorn src.main:app --host 0.0.0.0 --port 8000
