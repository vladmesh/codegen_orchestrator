#!/bin/bash
# Creates the 'langfuse' database for Langfuse v3 tracing.
# Mounted into /docker-entrypoint-initdb.d/ — runs only on fresh volumes.
# For existing deployments: make init-langfuse-db

set -e

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-EOSQL
    SELECT 'CREATE DATABASE langfuse'
    WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'langfuse')\gexec
EOSQL
