# Audit Report

## Environment & Setup

### What worked well
- `make validate-specs` caught issues early before code generation
- `make generate-from-spec` correctly generated Pydantic schemas, controller protocols, and controller stubs from YAML specs
- `make lint` runs ruff, xenon (complexity), spec validation, spec compliance, and controller sync checks — comprehensive
- `make tests backend EXEC_MODE=native` worked out of the box with SQLite test database
- The conftest.py pattern with SQLite + transactional rollback per test is solid and fast

### Framework Code Generation

**Positive:**
- The spec-first workflow is well-structured: define models in YAML, define operations in domain YAML, generate, implement
- Controller stubs with `NotImplementedError` are a great starting point — clearly marks what needs implementation
- Protocol generation ensures controllers stay in sync with specs (`make lint-controllers`)
- The generated schemas correctly handle `readonly` fields (excluded from Create/Update variants), `optional` fields, and `default` values

**Observation — ORMBase has `updated_at` but Todo spec has no `updated_at` field:**
- `ORMBase` (in `services/backend/src/core/db.py`) adds both `created_at` and `updated_at` columns automatically
- The Todo spec only defines `created_at` as a readonly field
- This means the `todos` table will have an `updated_at` column from ORMBase, but the `TodoRead` schema does not expose it
- This is not a bug (the column exists, it's just not returned), but it could be confusing. Consider either: (a) always including `updated_at` in models that use ORMBase, or (b) providing a way to opt out of `updated_at` in ORMBase

**Observation — `description` default handling:**
- The spec defines `description` with `default: ""`, but the generated `TodoCreate` schema makes it `Optional[str] = None` (because of `optional: [description]` in the Create variant), not `str = ""`
- This means if a client omits `description`, the schema sends `None` rather than `""` to the controller. The ORM model uses `server_default=text("''")` to handle this at the DB level, but the Pydantic model and the in-memory object may briefly have `None`. This worked fine, but the interaction between spec `default` and variant `optional` could be documented more clearly.

### Documentation
- AGENTS.md is in Russian — this works but could be a barrier for non-Russian-speaking contributors
- The example router in AGENTS.md was very helpful and directly applicable
- The `CONTRIBUTING.md` referenced in AGENTS.md does not exist in the repo

### Lint & Formatting
- ruff catches import sorting (I001) and line length (E501) — helpful for consistency
- `make format EXEC_MODE=native` can auto-fix most issues, but it's not automatically run before `make lint`
- The xenon complexity checker is a nice touch for keeping code quality high

### Testing
- Tests use `pytest-asyncio` with `AsyncClient` backed by `ASGITransport` — clean pattern
- The conftest.py mock for the event broker (`shared.generated.events._broker`) is a practical approach but relies on internal naming (`_broker`, `_pub_*`) which could break if the framework changes its internals

### Minor Issues
- No issues with `EXEC_MODE=native` — all make targets worked correctly
- Git hooks are configured via `.githooks/` directory and `core.hooksPath` — works well

### Suggestions for Improvement
1. **Add `make format` to pre-lint step** — Running format before lint would catch import sorting and line length issues automatically
2. **Document the variant/default interaction** — Clarify how spec `default` values interact with variant `optional` fields in the generated schemas
3. **Consider adding a `make new-domain` scaffolding command** — Automate creation of the domain YAML, since the structure is well-defined
4. **Add English translations to AGENTS.md** — Or at minimum provide an English version alongside the Russian one
5. **Include `CONTRIBUTING.md`** — Referenced in AGENTS.md but missing from the repo

