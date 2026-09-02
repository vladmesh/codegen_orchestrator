# Contributing

Use the generated Makefile as the project command surface.

```bash
make setup
make lint
make typecheck
make tests
```

For spec changes, run `make validate-specs` and `make generate-from-spec` before verification.

## Ownership

Never edit framework-generated paths:

- `shared/shared/generated/`
- `services/*/src/generated/`

Copier always preserves the paths listed in `_skip_if_exists`: local env files, shared model/event
specs, service application code, and controllers. Other template files participate in Copier's
update merge; review the update diff and resolve conflicts. A whole service directory is neither
globally read-only nor globally protected.

## Adding a service

There is no service-scaffolding command. For an arbitrary service, create its directory and update
`services.yml`, Compose, environment contracts, dependencies, and tests manually. Adding a
previously excluded predefined Copier module to an existing project is not currently automated.

## API changes

1. Edit `shared/spec/models.yaml` or `services/<name>/spec/*.yaml`.
2. Run `make validate-specs` and `make generate-from-spec`.
3. Implement controller, persistence, and wiring changes in user-owned paths.
4. Commit specs and regenerated artifacts together.

## Quality rules

- Use type hints and absolute imports across package boundaries.
- Let Ruff format and sort imports.
- Keep complexity below the Xenon thresholds enforced by `make lint`.
- Keep service runtime dependencies in the service that imports them; `make check-deps` runs deptry
  where installed.
- Use timezone-aware datetimes such as `datetime.now(UTC)`.

## Environment variables

Required application runtime settings must not use fallback values. A missing required value must
fail with a clear error and be documented in `.env.example`.

Defaults are allowed for local Compose interpolation and isolated test fixtures. They describe
local infrastructure behavior and must not become silent production application defaults.

## Backend runtime

The backend lifespan owns the lazy event broker connection. Generated publishers use
`get_broker()`; do not create or connect a broker inside request handlers.

The database dependency owns commit and rollback. Controllers must not commit transactions.
