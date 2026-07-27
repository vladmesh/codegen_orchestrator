#!/usr/bin/env bash
set -euo pipefail

: "${GRAFANA_DB_USER:?GRAFANA_DB_USER must be set}"
: "${GRAFANA_DB_PASSWORD:?GRAFANA_DB_PASSWORD must be set}"

export PGPASSWORD="${POSTGRES_PASSWORD}"

psql --username "${POSTGRES_USER}" --dbname "${POSTGRES_DB}" \
  --set=grafana_db_user="${GRAFANA_DB_USER}" \
  --set=grafana_db_password="${GRAFANA_DB_PASSWORD}" <<'SQL'
SELECT format('CREATE ROLE %I LOGIN PASSWORD %L NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT',
              :'grafana_db_user', :'grafana_db_password')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'grafana_db_user')
\gexec

SELECT format('ALTER ROLE %I PASSWORD %L NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT',
              :'grafana_db_user', :'grafana_db_password')
\gexec

SELECT format('GRANT CONNECT ON DATABASE %I TO %I', current_database(), :'grafana_db_user')
\gexec
GRANT USAGE ON SCHEMA public TO :"grafana_db_user";
GRANT SELECT ON ALL TABLES IN SCHEMA public TO :"grafana_db_user";
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO :"grafana_db_user";
SQL
