# Backend service

FastAPI application with SQLAlchemy, Alembic, generated REST adapters, and optional event
publishing.

## Contract workflow

- Shared models: `shared/spec/models.yaml`
- Domain operations: `services/backend/spec/*.yaml`
- Generated shared contracts: `shared/shared/generated/`
- Generated backend adapters: `services/backend/src/generated/`
- Editable controllers: `services/backend/src/controllers/`
- Manual application wiring: `services/backend/src/app/`

After changing a spec:

```bash
make validate-specs
make generate-from-spec
make lint
make tests backend
```

The generated router registry is composed by `src/app/api/router.py`. Do not create a parallel
manual router for an operation already owned by a domain spec.

## Database migrations

When adding a handwritten ORM model, register it explicitly in `src/app/models/registry.py` so
Alembic includes it in `Base.metadata`.

```bash
make makemigrations name="describe_change"
make migrate
```

By default these targets start PostgreSQL through the dev Compose layers and run Alembic in a
one-off backend container. `make dev-start` also applies pending migrations before starting the API.

`make migrate` applies the core head first and then each active package's Alembic revisions in
manifest order. Keep product-local package wheels in `services/backend/packages/` so backend image
builds can install the locked dependency before application sources are copied.

For an already reachable PostgreSQL instance, install the backend environment and bypass Compose:

```bash
uv sync --project services/backend
make SKIP_INFRA_START=1 POSTGRES_HOST=localhost POSTGRES_PORT=5432 migrate
```

Pass `DATABASE_URL=...` as a Make variable when a full URL is preferable.
