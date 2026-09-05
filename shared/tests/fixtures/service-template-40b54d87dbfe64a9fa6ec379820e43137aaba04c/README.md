# env_fixture

A microservice project built with service-template framework

## Quick Start

1. **Setup project** (installs dependencies, generates code, configures git hooks)
   ```bash
   make setup
   ```
   Fresh generated projects are not importable until this completes because
   `make setup` creates the generated modules under `shared/shared/generated/`
   and `services/*/src/generated/`.

2. **Configure environment**
   ```bash
   cp .env.example .env
   # Edit .env with your settings
   ```

### Development Ports

`make dev-start` includes `infra/compose.local.yml`, which publishes host ports from `.env`:
- `BACKEND_PORT` maps the backend HTTP service to container port `8000`.
- `POSTGRES_HOST_PORT` maps PostgreSQL to container port `5432`.
- `REDIS_HOST_PORT` maps Redis to container port `6379`.
- `FRONTEND_PORT` maps the frontend to container port `4321`.


Set different host ports when running multiple generated projects on one machine.
Use `infra/compose.base.yml` plus `infra/compose.dev.yml` without the local layer when services
should stay reachable only on the Compose network.
Use `make worker-start`, `make smoke-probe`, `make worker-call`, and `make worker-stop`
for that port-less worker flow. `worker-call` runs an HTTP request from a one-off
service container inside the Compose network:

```bash
make worker-start
make worker-call url=http://backend:8000/health method=GET
make worker-stop
```

`POST /users/grant` and `POST /users/revoke` are deployment capabilities, not worker-call
commands. They require the single `X-Grant-Capability` request header. The local `.env` fixture
only makes the generated development stack start; deployment receives its distinct per-project
value as the backend's `USERS_GRANT_CAPABILITY` generated secret, which is never committed to this
project.

`POST /settings/set` is the equivalent write capability for manifest-declared product settings.
It requires one `X-Settings-Capability` header. Product values such as user-selected languages
belong in `services/<service>/manifest.yaml`, are validated and persisted through
`/settings/set`, and are never copied into environment variables.

`POST /jobs/fire` is the equivalent capability for firing a manifest-declared behaviour by name. It
requires one `X-Jobs-Capability` header; `POST /jobs/evidence` reads the recorded evidence back and
requires none.

3. **Start development**
   ```bash
   make dev-start
   ```
   Use `make ps` to see the current project's Compose stack status.
   Use `make dev-stop` to stop containers while keeping volumes, and `make dev-clean`
   to remove this Compose project's containers, network, and volumes.

4. **Run tests**
   ```bash
   make tests
   ```
   Run `make setup` before `make lint` or `make tests`; those targets expect
   the root and service virtual environments to exist.

## Modules

This project includes the following modules:

- **backend** - FastAPI REST API with PostgreSQL
- **tg_bot** - Telegram bot (python-telegram-bot polling)
- **notifications** - Notification worker (email, telegram)
- **frontend** - Node.js frontend


## Development Workflow

- **Add/modify models:** Edit `shared/spec/models.yaml` → `make generate-from-spec`
- **Add endpoints:** Edit `services/backend/spec/*.yaml` → `make generate-from-spec`
- **Add domain operations:** declare them in the spec, then implement their generated controller
  protocol and repository boundary.
- **Add product settings:** declare independent JSON Schema properties in
  `services/<service>/manifest.yaml`, run `make generate-from-spec`, then use the typed
  `/settings/get` and `/settings/set` core operations.
- **Add a fireable behaviour:** declare its name and argument schema under `jobs_schema` in
  `services/<service>/manifest.yaml`, run `make generate-from-spec`, then fire it by name through
  `/jobs/fire`. A module that performs the work subscribes to `job_fired` and declares
  `provides: ["jobs.fire"]`.
- **Create database migrations:** add or change ORM models, then run
  `make makemigrations name="describe_change"`. The target starts the PostgreSQL dev container
  through the worker compose layer and runs Alembic inside a one-off backend container. It upgrades the database to the current
  head before autogeneration, and writes the revision to `services/backend/migrations/versions/`.
  Without Docker, use `uv sync --project services/backend`, make sure PostgreSQL is already
  reachable, then run `make SKIP_INFRA_START=1 makemigrations name="describe_change"`.
- **Apply database migrations:** run `make migrate` or `make dev-start`.
- **Run linter:** `make lint`
- **Run formatter:** `make format`

## Documentation

- [ARCHITECTURE.md](ARCHITECTURE.md) - System design
- [CONTRIBUTING.md](CONTRIBUTING.md) - Coding standards

## Tech Stack

- **Python** 3.12
- **FastAPI** + **Pydantic** (spec-first)
- **PostgreSQL** + **SQLAlchemy** + **Alembic**
- **Redis** + **FastStream** (async messaging)
- **Node.js** 20
- **Docker Compose** (containerized development)

---

*Generated with [service-template](https://github.com/your-org/service-template)*
