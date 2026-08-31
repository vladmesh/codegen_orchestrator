# Infrastructure contract

This directory contains the Docker Compose files that define the generated
project runtime. Keep these files compatible with two callers:

- local developers running `make dev-start`;
- external orchestrators that run generated projects as sibling containers and
  attach them to an already-created worker network.

## Service names

Compose service names are part of the project API. Other containers resolve
services by these DNS names on the default Compose network:

- `db` is PostgreSQL on port `5432` when the `backend` module is enabled.
- `redis` is Redis on port `6379` when `backend`, `tg_bot`, or `notifications`
  is enabled.
- `backend` is the FastAPI service on port `8000` when the `backend` module is
  enabled.
- `tg_bot`, `notifications_worker`, and `frontend` are the optional application
  services for their modules.

Do not rename these services without updating callers that use Compose DNS.
For example, generated defaults use `POSTGRES_HOST=db` and
`REDIS_URL=redis://redis:6379`.

## Network rules

Do not declare custom networks in these compose files and do not rename the
implicit `default` network. External worker managers may replace the generated
project's default network with a pre-created network shared with the worker
container. This lets the worker reach `db:5432`, `redis:6379`, and
`backend:8000` directly without Docker-in-Docker or host port forwarding.

## Compose modes

Worker mode uses the base and dev layers only:

```bash
make worker-start
```

That target uses `-f infra/compose.base.yml -f infra/compose.dev.yml` and does
not include `infra/compose.local.yml`.

This starts every service in the selected module set without the local-port
layer. Pass `svc=...` to start only selected services. Backend projects have
both `db` and `redis`; bot-only or notifications-only projects have `redis` but
no `db`.

For a backend plus notifications project, a full worker-mode smoke run is:

```bash
make worker-start
make smoke-probe
make worker-call url=http://backend:8000/health method=GET
make worker-stop
```

`make smoke-probe` defaults to `http://backend:8000/health` when the backend
module is present. To probe another in-network URL, pass both the Python service
used as a one-off runner and the URL:

```bash
make smoke-probe SMOKE_RUNNER=backend SMOKE_URL=http://backend:8000/health
```

Use `make worker-call` for non-GET requests:

```bash
make worker-call SMOKE_RUNNER=backend url=http://backend:8000/health method=GET
```

Worker mode must not depend on published host ports. The dev layer keeps
services reachable on the Compose network, and the local-port layer is omitted.
`make migrate` and `make makemigrations name="..."` use this worker-mode layer
for their Docker path. Use `SKIP_INFRA_START=1` only when the database is
already reachable from the host process.

Local developer mode adds published ports:

```bash
make dev-start
```

This expands to:

```bash
docker compose \
  -f infra/compose.base.yml \
  -f infra/compose.dev.yml \
  -f infra/compose.local.yml \
  up -d --build --wait
```

`infra/compose.local.yml` is the only layer that publishes development ports to
the host. Configure those ports in `.env`:

- `BACKEND_PORT` maps to `backend:8000`.
- `POSTGRES_HOST_PORT` maps to `db:5432`.
- `REDIS_HOST_PORT` maps to `redis:6379`.
- `FRONTEND_PORT` maps to `frontend:4321`.

Production mode uses:

```bash
docker compose \
  -f infra/compose.base.yml \
  -f infra/compose.prod.yml \
  up -d --remove-orphans
```

`infra/compose.prod.yml` publishes the backend on `${BACKEND_PORT}:8000` so the
deployed service is reachable at `http://<host>:${BACKEND_PORT}/health` from
outside the Compose network. `BACKEND_PORT` is required in this mode (no default)
so co-located apps on the same host must each pick a unique host port.

Integration-test mode uses `infra/compose.tests.integration.yml` directly
through `make test-integration`.

## Parallel run isolation

Set a unique Compose project name when running multiple generated projects on
one Docker host:

```bash
COMPOSE_PROJECT_NAME=my-project-dev make dev-start
```

The generated `.env` and `.env.example` include a commented
`COMPOSE_PROJECT_NAME` line for this. Pair it with distinct published host
ports in `.env` when the local-port layer is enabled:

- `POSTGRES_HOST_PORT` for PostgreSQL.
- `REDIS_HOST_PORT` for Redis.
- `BACKEND_PORT` for the backend HTTP service.
- `FRONTEND_PORT` for the frontend.

Use `make ps` and `make log <service>` to inspect the selected Compose project.
For local work, prefer the Makefile targets. They include and export the
generated `.env` before invoking Compose.

When running Compose by hand from the project root, include `--env-file .env`
or export the same variables in your shell. Without that, the top-level Compose
`name:` falls back to the generated project slug.

```bash
docker compose --env-file .env \
  -f infra/compose.base.yml \
  -f infra/compose.dev.yml \
  run --rm --no-deps backend \
  python -c 'import urllib.request; urllib.request.urlopen("http://backend:8000/health")'
```

`make worker-clean` runs `docker compose down --volumes --remove-orphans` with
the base and dev layers only. `make dev-clean` does the same with the local
compose layer included. Both remove only resources under the selected Compose
project name.

External sibling-container orchestrators should keep passing Compose's
`--project-name` option explicitly when they need deterministic project names.
That mode still uses the base and dev layers without published host ports.

## Healthchecks

Infrastructure services must have healthchecks. External orchestrators and the
generated Makefile use `docker compose up --wait`, so readiness is defined by
Compose health state, not by sleep loops.

Current required healthchecks:

- `db`: `pg_isready` against the configured database.
- `redis`: `redis-cli ping`.
- `backend`: HTTP `GET /health`.

Do not remove these healthchecks when changing service definitions. If a new
infrastructure service is added, give it a healthcheck before depending on it
from application services or orchestration code.

## Database and Redis environment

Environment priority depends on where the value is consumed:

1. Compose interpolation values, such as `${POSTGRES_HOST:-db}`, use the shell
   or command environment first, then generated `.env`, then the default in the
   compose file.
2. Container variables loaded with `env_file` come from generated `.env`.
3. Local `make` commands include and export generated `.env`. Make command-line
   variables override it. A shell-prefix value before `make` does not override a
   variable already assigned by `.env` unless you opt into GNU Make environment
   precedence with `make -e`.
4. Local Python commands use process environment for required application settings; missing
   required values fail instead of falling back silently.

For local `make` targets, `.env` is included and exported by the generated
Makefile. Pass overrides as make variables:

```bash
make POSTGRES_HOST=custom-db POSTGRES_PORT=6543 migrate
```

PostgreSQL can be configured by parts in Compose modes and by full URL for
local process commands:

- `POSTGRES_HOST` and `POSTGRES_PORT` select the host and port used to build
  Compose database URLs. The generated `.env` uses `db` and `5432`.
- `POSTGRES_DB`, `POSTGRES_USER`, and `POSTGRES_PASSWORD` complete the Compose
  database URLs.
- `POSTGRES_REQUIRE_SSL` is read by backend settings when backend code builds a
  URL from parts outside Compose. The Compose-built `DATABASE_URL` and
  `ASYNC_DATABASE_URL` entries do not append `sslmode=require`.
- `DATABASE_URL` overrides the sync SQLAlchemy URL when backend code runs
  outside Compose.
- `ASYNC_DATABASE_URL` overrides the async SQLAlchemy URL when backend code runs
  outside Compose.

Use `POSTGRES_HOST=db` inside Compose worker/local modes. Use `localhost` or
another reachable host only when running backend tooling outside the Compose
network, for example with `SKIP_INFRA_START=1`. In worker/local Compose modes,
`DATABASE_URL` and `ASYNC_DATABASE_URL` are built by `compose.base.yml` from the
`POSTGRES_*` parts; shell values for the full URLs do not replace those service
environment entries.

Redis is configured by full URL:

- `REDIS_URL` is the application Redis endpoint. The generated `.env` uses
  `redis://redis:6379` when Redis is part of the selected modules.
- `TEST_REDIS_URL` overrides the Redis endpoint used by pytest fixtures.
- If `TEST_REDIS_URL` is not set, tests fall back to `TEST_REDIS_HOST` and
  `TEST_REDIS_PORT`, then to `localhost:6379`.

Use `REDIS_URL=redis://redis:6379` inside Compose worker/local modes. Use
`TEST_REDIS_URL` when Redis tests should target an isolated database or an
externally managed Redis instance. In worker/local Compose modes, Redis
consumers load `REDIS_URL` from generated `.env`; a shell `REDIS_URL=... docker
compose ...` invocation does not override the container value unless `.env` or
the compose service environment is changed.
