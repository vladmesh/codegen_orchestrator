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
3. Edit `services/<service>/manifest.yaml` for user-controlled settings schemas and for the
   `jobs_schema` of fireable behaviours; it is separate from domain generation and must use the
   documented fail-closed Draft 2020-12 form.
4. Run `make validate-specs` and `make generate-from-spec`.
5. Implement or update the controller under `src/controllers/`.
6. Update manual ORM models, repositories, application wiring, and migrations as needed.
7. Run `make lint`, `make typecheck`, and `make tests backend`.

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

`POST /settings/set` similarly requires exactly one `X-Settings-Capability` matching the generated
`SETTINGS_WRITE_CAPABILITY`. It validates only manifest-declared values and is the sole product
settings write path. Never place product setting values, schemas containing secrets, or this
credential in environment variables, OpenAPI, logs, or LLM-facing data.

`POST /jobs/fire` requires exactly one `X-Jobs-Capability` matching the generated
`JOBS_FIRE_CAPABILITY`, and fires only a name declared under `jobs_schema`. The core records the
command under its `(fired_by_product, command_id)` identity, commits it, and only then emits
`job_fired` from a single place holding that row's lock; it never resolves which module executes
the behaviour. The one loop the core runs is the timer loop in `src/app/timers.py`: the lifespan
starts it only when the generated `JOB_TIMERS` (package-declared timers) is non-empty, and it fires
each timer job once per slot through the same `JobsController.fire` with `{"at": <slot instant>}`,
identity `core-timer:<job>:<slot>` and `fired_by_run="core-timer"`. A failed fire is logged and the
loop goes on to the next slot. Do not add a second dispatch path or a scheduler beside it.
`POST /jobs/evidence` reads that evidence back and carries no capability. Never put this credential, or any secret, in a
URL, an event payload, an error body, or a log line.

### Caller identity on package routes

A package route that acts for a user depends on `codegen_kit.caller_identity`
(`src/app/caller_identity.py`); it is the only place a user identity is taken from a request. This
is service-level trust inside the product: the trusted caller is an in-product service, the tg_bot,
which presents exactly one `X-Identity-Capability` matching the generated
`USER_IDENTITY_CAPABILITY` together with `X-User-Channel` and `X-User-External-Id` naming the user
it acts for, taken from the real Telegram update. A request is not trusted because of where it
comes from. The dependency compares the capability in constant time, validates the channel (no
`:`, at most 64 characters) and external id (at most 256), resolves them in `user_channels` and
requires the user to be `active`:

- a missing, repeated, wrong or non-ASCII capability, or a missing, repeated, empty or malformed
  channel or external id answers **401**;
- an unknown identity or an inactive user answers **403**;
- otherwise the route receives the canonical `user_ref`, `"<channel>:<external_id>"`, for example
  `telegram:123456`.

Never accept a user reference from a body, query or path on a package route, never add a second
identity check beside the dependency, and never wrap the product's own routes in a global identity
middleware. The identity headers stay out of OpenAPI, and the capability out of logs, URLs, event
payloads and error bodies.

## Database and migrations

`get_async_db()` owns commit, rollback, and close. Controllers must not call `session.commit()`,
with one deliberate exception: the jobs core commits a recorded command before it emits `job_fired`,
because an event must never exist for a command no committed row records.
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
