# AGENTS — Backend API

## Scope and ownership

Backend code, specs, migrations, tests, and dependencies live under `services/backend/`. Shared
Pydantic and event contracts live under `shared/`.

Do not edit generated files:

- `shared/shared/generated/`
- `services/backend/src/generated/`

User-owned implementation lives in `services/backend/src/app/`, `src/controllers/`, migrations,
and tests. Run commands from the project root through `make`.

## Spec-first workflow

1. Edit `shared/spec/models.yaml` for shared data shapes.
2. Edit `services/backend/spec/<domain>.yaml` for operations and transports.
3. Run `make validate-specs` and `make generate-from-spec`.
4. Implement or update the controller under `src/controllers/`.
5. Update manual ORM models, repositories, application wiring, and migrations as needed.
6. Run `make lint`, `make typecheck`, and `make tests backend`.

Generation owns Pydantic schemas, protocols, REST routers, the router registry, and event adapters.
Controller stubs are created only when missing and are editable afterwards. Manual
`src/app/api/router.py` composes the generated registry with infrastructure endpoints.

Never create a second Pydantic model for a shape owned by `models.yaml`. Domain-specific validation
that is not part of a shared contract may remain manual.

## Imports

Use absolute imports across package boundaries:

```python
from services.backend.src.controllers.users import UsersController
from services.backend.src.core.db import get_async_db
from services.backend.src.generated.protocols import UsersControllerProtocol
from shared.generated.schemas import UserAccess, UserGrant
```

Relative imports are acceptable within one package. Do not import from a top-level `src` package.

## Security invariant

`User.status` is the sole persisted admission decision. `users.grant(channel, external_id)` is the
only operation that creates or activates an external identity; `users.revoke(channel, external_id)`
only deactivates an existing identity. The bot resolves that identity and admits only `active` users.
`POST /users/grant` and `POST /users/revoke` require exactly one
`X-Grant-Capability` header whose value matches the generated-secret
`USERS_GRANT_CAPABILITY` using a constant-time comparison. This header is deliberately absent from
OpenAPI and from user-facing helper commands. Do not introduce environment, owner, or channel-
specific fallback admission paths.

## Database and migrations

`get_async_db()` owns commit, rollback, and close. Controllers must not call `session.commit()`.
Add every new handwritten ORM model to `src/app/models/registry.py`; Alembic imports that explicit
user-owned registry before reading `Base.metadata`.

```bash
make makemigrations name="describe_change"
make migrate
```

These targets use the dev Compose database by default. With an already reachable PostgreSQL
instance, use `SKIP_INFRA_START=1` and pass the host/port or `DATABASE_URL` as Make variables.

## Events

The application lifespan connects and closes the lazy broker returned by `get_broker()`. Generated
publishers obtain that broker internally. Do not create another broker or connect inside handlers.

Operation-level `events:` configuration controls subscriptions and success/error publications.
The generated REST and event adapters delegate to the same controller protocol.

## Commands

| Command | Purpose |
|---|---|
| `make generate-from-spec` | Regenerate contract-owned artifacts |
| `make validate-specs` | Validate YAML specs |
| `make lint-controllers` | Check controllers against protocols |
| `make openapi` | Export OpenAPI |
| `make makemigrations name="..."` | Create an Alembic migration |
| `make migrate` | Apply migrations |
| `make tests backend` | Run backend tests |
