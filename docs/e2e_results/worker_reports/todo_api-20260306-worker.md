# Audit Report

## Overall Assessment

The framework and development environment worked well. The spec-first workflow is smooth and the code generation is reliable.

## What Worked Well

1. **Spec-first workflow**: Editing `models.yaml` + domain spec YAML, then running `make generate-from-spec` correctly produced Pydantic schemas, protocols, and controller stubs. No manual schema creation needed.
2. **Code generation**: The framework generated correct `TodosControllerProtocol`, `TodoCreate`/`TodoUpdate`/`TodoRead` schemas, and a controller stub with `NotImplementedError` placeholders.
3. **Validation tooling**: `make validate-specs` caught issues early. `make lint-controllers` verified controller-protocol sync.
4. **Test infrastructure**: The `conftest.py` with SQLite + transactional rollback worked seamlessly for the new Todo model without any modifications.
5. **Migration workflow**: `orchestrator dev-env start-infra db` + `make migrate` + `make makemigrations` worked correctly end-to-end.
6. **Linting**: `make lint` and `make format` with ruff worked smoothly.

## Issues Encountered

1. **Formatting inconsistency in generated controller**: The generated controller stub (`services/backend/src/controllers/todos.py`) had empty docstrings (`""" """`) in each method. While not a bug, it's slightly odd - either generate meaningful docstrings or omit them.

2. **No router generation**: The framework generates protocols and controller stubs but not routers. Routers must be written manually and registered in `router.py`. This is documented in AGENTS.md but could be a candidate for generation since the spec contains all the REST metadata (method, path, status code).

3. **Model spec `default` for optional Create fields**: When using `optional: [description]` in the Create variant, the generator correctly makes `description` optional (`str | None = None`). However, the ORM model needs a non-null `server_default` which requires the developer to know to handle the `None` -> `""` conversion in the repository. A `default_on_create` spec field could simplify this.

4. **ORMBase vs Base+Mixin choice not guided**: The spec defines which fields a model has (`created_at` only for Todo vs `created_at` + `updated_at` for User), but the developer must manually choose between `ORMBase` (which adds both timestamps) and `CreatedAtMixin + Base`. The framework could generate ORM model stubs or at least document the mixin selection guidance.

5. **`ruff format` needed after manual file creation**: New files created during implementation weren't auto-formatted. Running `make format` fixed it, but a pre-commit hook that formats on save would be smoother. The git hooks run on commit which catches this, but it would be nice to have guidance that `make format` should be run periodically during development.

## Issues Encountered (Adding Stats Endpoint)

6. **No spec-first mechanism for aggregation endpoints**: The AGENTS.md rule says "Never create BaseModel manually — schemas are generated from models.yaml." However, for the `GET /todos/stats` endpoint returning `{"total": N, "completed": N, "pending": N}`, there's no way to define this response shape in the spec YAML since it's not a CRUD model. I had to create a local `TodoStats(BaseModel)` in the router file as a pragmatic workaround.

7. **Protocol doesn't cover custom endpoints**: The generated `TodosControllerProtocol` only includes spec-defined CRUD operations. The `get_stats` method added to `TodosController` is not part of the protocol, meaning custom endpoints bypass the spec-first contract. The `lint-controllers` check still passes (it only verifies protocol methods are implemented, not that extra methods exist), which is reasonable behavior.

8. **`shared/generated/schemas.py` path confusion**: The actual file is at `shared/shared/generated/schemas.py` (double-nested Python packaging convention), but imports use `from shared.generated.schemas import ...`. This is documented but can be confusing when navigating the filesystem manually.

## Suggestions

1. **Generate routers from spec**: Since the spec contains REST method, path, status code, and parameter info, router files could be auto-generated (or at least scaffolded) to reduce boilerplate.
2. **Generate ORM model stubs**: Based on the model fields in `models.yaml`, the framework could scaffold ORM models with the correct base class and column types.
3. **Add a `make new-domain name=todos` command**: A single command that creates the spec YAML, generates code, and scaffolds the router/repository/ORM model would streamline adding new domains.
4. **Document the CreatedAtMixin vs ORMBase decision**: The AGENTS.md could include guidance on when to use which base class based on whether the model needs `updated_at`.
5. **Support non-CRUD response schemas in spec**: Allow defining response-only schemas (e.g., `TodoStats`) in the spec YAML for aggregation/stats endpoints, so they can be generated rather than hand-written.
6. **Support custom operations in domain specs**: Allow defining operations that don't map to standard CRUD (e.g., stats, bulk operations) so they get proper protocol methods and type safety.

