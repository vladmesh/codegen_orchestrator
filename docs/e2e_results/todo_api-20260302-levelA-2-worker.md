# Audit Report

## Environment & Tooling

### `orchestrator dev-env start-infra db` fails
- **What happened**: Running `orchestrator dev-env start-infra db` fails with:
  ```
  stat /tmp/codegen/workspaces/.../workspace/docker-compose.yml: no such file or directory
  ```
- **Expected**: The orchestrator should find the compose files in `infra/` directory, as that's where they live per the project scaffolding.
- **Root cause**: The orchestrator looks for `docker-compose.yml` in the repo root, but the project uses `infra/compose.base.yml`, `infra/compose.dev.yml`, etc.
- **Workaround**: Wrote the Alembic migration manually instead of using `make makemigrations`, since that requires a running PostgreSQL instance.
- **Suggestion**: Either have the scaffolding create a root-level `docker-compose.yml` symlink/include, or configure the orchestrator to look in `infra/`.

### `make makemigrations` requires running DB
- **What happened**: Cannot use `make makemigrations name="..."` without a running PostgreSQL database, because Alembic's autogenerate needs a live DB connection.
- **Suggestion**: Document this dependency more prominently. Consider supporting SQLite-based migration generation for development, or provide a lightweight alternative that doesn't require infrastructure.

## Spec-First Workflow

### Code generation works well
- The `make generate-from-spec` workflow worked smoothly. Adding models to `shared/spec/models.yaml` and a domain spec to `services/backend/spec/todos.yaml` correctly generated:
  - Pydantic schemas in `shared/shared/generated/schemas.py`
  - Protocol class in `services/backend/src/generated/protocols.py`
  - Controller stub in `services/backend/src/controllers/todos.py`
- The generated controller stub has all correct method signatures with `NotImplementedError` placeholders.

### `TodoCreate` variant `exclude` behavior
- Setting `exclude: [is_completed]` on the `Create` variant correctly excluded `is_completed` from `TodoCreate` while keeping `title` and `description`.
- The `default` values from the model spec are properly reflected in generated schemas.

### Spec validation and compliance checks are helpful
- `make validate-specs` and `make lint` (including `enforce_spec_compliance` and `lint-controllers`) caught issues early and confirmed everything was synchronized.

## Framework Observations

### ORMBase provides `created_at` and `updated_at` automatically
- The `ORMBase` class in `services/backend/src/core/db.py` adds `created_at` and `updated_at` with `server_default=func.now()`. This is well-designed but means every ORM model inheriting `ORMBase` gets `updated_at` even if the spec model doesn't define it (e.g., `Todo` spec only has `created_at`). The extra `updated_at` column is harmless but adds a slight spec-to-DB mismatch.

### SQLite test setup works but needs UTC workaround
- The existing `conftest.py` uses SQLite for tests, which returns naive datetimes. The controller needs a manual UTC timezone fix (`_to_schema` helper pattern). This is a known limitation documented in the existing User controller.

### AGENTS.md is in Russian
- All documentation in `AGENTS.md` and `services/backend/AGENTS.md` is in Russian. This may be by design but could limit accessibility for non-Russian-speaking developers.

## Suggestions

1. **Fix orchestrator compose file discovery** — Most impactful issue. The `orchestrator dev-env` commands should work with the actual project layout.
2. **Add a lightweight migration generation path** — Allow `make makemigrations` to work without a running DB, or provide clear guidance on when to write migrations manually.
3. **Consider an ORMBase variant without `updated_at`** — For models that only need `created_at`, provide `ORMBaseReadOnly` or similar.
4. **English documentation option** — Offer English translations of AGENTS.md for broader team accessibility.

