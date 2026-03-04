# Audit Report

## Framework & Development Environment Audit

### Overall Assessment
The framework works well for the spec-first workflow. The code generation pipeline (`models.yaml` → `schemas.py`, `spec/*.yaml` → `protocols.py` + controller stubs) is smooth and effective.

### What Worked Well

1. **Spec-first code generation**: Running `make generate-from-spec` correctly produced Pydantic schemas (`TodoCreate`, `TodoRead`, `TodoUpdate`), protocol classes (`TodosControllerProtocol`), and controller stubs (`TodosController` with `NotImplementedError` placeholders). Very smooth workflow.

2. **Spec validation**: `make validate-specs` caught issues early before generation. The linter `make lint-controllers` verified controller-protocol synchronization — a nice safety net.

3. **Database migration workflow**: `orchestrator dev-env start-infra db` → `make migrate` → `make makemigrations name="..."` → `make migrate` worked perfectly. Alembic autogenerate correctly detected the new `todos` table.

4. **Test infrastructure**: The `conftest.py` with SQLite-based transactional test isolation is well-designed. Adding tests for a new domain was straightforward — just create a new test file and use the existing `client` fixture.

5. **Lint tooling**: All linters (`ruff`, `xenon`, spec validation, spec compliance, controller sync) ran cleanly and caught real issues (import sorting, line length).

### Issues Encountered

1. **Todo model needed `Base` instead of `ORMBase`**
   - **File**: `services/backend/src/app/models/todo.py`
   - **Issue**: The `ORMBase` abstract class provides both `created_at` and `updated_at` columns. The Todo spec only defines `created_at` (no `updated_at`), so using `ORMBase` would add an unexpected column. Had to use `Base` directly and define `created_at` manually.
   - **Suggestion**: Consider either (a) making `ORMBase` configurable (e.g., opt-in to `updated_at`), or (b) documenting that models without `updated_at` should extend `Base` directly.

2. **No router code generation**
   - **Issue**: The framework generates protocols and controller stubs, but routers must be written manually. This is documented in `AGENTS.md` with examples, which helps, but it's still the most boilerplate-heavy part.
   - **Suggestion**: Consider generating router stubs similar to controller stubs, since the router pattern is very formulaic (map HTTP method to controller method with the right `Depends` wiring).

3. **`schemas/__init__.py` re-exports are manual**
   - **File**: `services/backend/src/app/schemas/__init__.py`
   - **Issue**: After adding new models, the `__init__.py` re-export must be updated manually. Easy to forget.
   - **Suggestion**: Either generate this file or remove the re-export pattern in favor of direct imports from `shared.generated.schemas`.

### Suggestions for Improvement

1. **Router generation**: The router pattern is repetitive and predictable. Generating router stubs (like controller stubs) would save time and reduce errors.

2. **Model base class flexibility**: Provide a `TimestampedBase` with only `created_at` as an alternative to `ORMBase` which has both `created_at` and `updated_at`.

3. **`make format` before `make lint`**: The lint errors I hit (import sorting, line length) were auto-fixable. Consider having `make lint` run `ruff format --check` and `ruff check` (without `--fix`) so developers know to run `make format` first, or document this workflow more prominently.

