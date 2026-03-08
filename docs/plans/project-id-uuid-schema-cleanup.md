# Project ID → UUID + schema cleanup

> [!WARNING]
> Этот файл автогенерируется командой `make sync`. Не редактируйте вручную — изменения будут перезаписаны.

## Context

Project.id is currently a meaningful string (e.g. "codegen-orchestrator") — `String(255)` in SQLAlchemy. This is fragile: IDs are coupled to names, renaming requires FK cascade, and there's no validation. The task migrates to native PostgreSQL UUID. DB is nearly empty — best time for breaking changes.

Additional cleanup: remove legacy `github_repo_id` and `repository_url` from Project (moved to Repository entity), add `visibility` to Repository, add `name` to ProjectUpdate API schema.

## Steps

1. [ ] Update Project model — id to Uuid, remove legacy columns
   - **Input**: `shared/models/project.py`, `shared/models/base.py`
   - **Output**: `Project.id` uses `sqlalchemy.Uuid` with `uuid4` default. Columns `github_repo_id` and `repository_url` removed. Import `uuid` added.
   - **Test**: Unit test — create Project instance, verify id is auto-generated UUID, verify github_repo_id/repository_url are absent
   - ⚠️ needs-approval (changes shared model)

2. [ ] Update all FK models — project_id String(255) → Uuid
   - **Input**: `shared/models/task.py`, `story.py`, `brainstorm.py`, `milestone.py`, `repository.py`, `run.py`, `port_allocation.py`, `service_deployment.py`, `api_key.py`
   - **Output**: All `project_id` columns use `Uuid` type matching `projects.id`. Non-FK columns (ServiceDeployment, APIKey) also changed.
   - **Test**: Unit test — verify FK columns accept UUID values, reject non-UUID strings
   - ⚠️ needs-approval (changes shared models)

3. [ ] Add visibility to Repository model
   - **Input**: `shared/models/repository.py`, `shared/contracts/dto/repository.py`
   - **Output**: `visibility: Mapped[str]` column with default "private". `RepositoryVisibility` enum (PUBLIC, PRIVATE) in DTO.
   - **Test**: Unit test — Repository instance defaults to "private", accepts "public"
   - ⚠️ needs-approval (changes shared model + contract)

4. [ ] Update shared contracts/DTOs
   - **Input**: `shared/contracts/dto/project.py`
   - **Output**: `ProjectCreate.id` type → `uuid.UUID | None` (auto-generated if None). `ProjectDTO.id` → `uuid.UUID`. `ProjectUpdate` — keep `name` field (already present). Remove `github_repo_id` from ProjectCreate/Update/DTO. Remove `repository_url` from ProjectDTO. Queue contracts (`shared/contracts/queues/`) — `project_id` stays `str` (UUID serialized).
   - **Test**: Unit test — ProjectCreate without id generates UUID, ProjectDTO serializes UUID correctly
   - ⚠️ needs-approval (changes shared contracts)

5. [ ] Update API schemas
   - **Input**: `services/api/src/schemas/project.py`
   - **Output**: `ProjectBase.id` → `uuid.UUID`. Remove `github_repo_id` and `repository_url` from ProjectBase/ProjectUpdate. Add `name` to ProjectUpdate. `ProjectCreate` — `id: uuid.UUID | None = None`. Repository schemas — add `visibility` field.
   - **Test**: Unit test — schema validation accepts UUID, rejects non-UUID for project_id

6. [ ] Update project router — remove legacy fields, UUID id handling
   - **Input**: `services/api/src/routers/projects.py`
   - **Output**: `create_project` — auto-generate UUID if not provided, remove github_repo_id assignment. `update/patch_project` — remove repository_url/github_repo_id handling, add name handling. Remove `get_project_by_repo_id` endpoint (legacy). `project_id` path params → `uuid.UUID` type annotation.
   - **Test**: Unit test — create project returns UUID id, update project accepts name, by-repo-id endpoint removed

7. [ ] Migrate webhook router from Project.github_repo_id to Repository.provider_repo_id
   - **Input**: `services/api/src/routers/webhooks.py`, `shared/models/repository.py`
   - **Output**: Webhook lookup: `select(Repository).where(Repository.provider_repo_id == repo_id)` → get project via `repository.project_id`. Remove `Project.github_repo_id` dependency entirely.
   - **Test**: Unit test — webhook finds project via Repository.provider_repo_id, returns 404 when no matching repo

8. [ ] Write Alembic migration
   - **Input**: `services/api/migrations/versions/`, all model changes from steps 1-3
   - **Output**: Single migration file that: (a) creates new UUID columns, (b) migrates existing string IDs → generated UUIDs with FK cascade, (c) drops legacy columns (github_repo_id, repository_url), (d) adds Repository.visibility. Includes `downgrade()`.
   - **Test**: `make migrate` succeeds, `make test-integration` passes (run after all code changes)

9. [ ] Update remaining routers — project_id type
   - **Input**: All routers that filter by project_id: `tasks.py`, `stories.py`, `brainstorms.py`, `milestones.py`, `repositories.py`, `allocations.py`, `service_deployments.py`, `runs.py`, `api_keys.py`
   - **Output**: Query parameter `project_id` type annotations updated where needed (FastAPI auto-coerces UUID strings). Repository router — add visibility to create/update schemas.
   - **Test**: Unit test — routers accept UUID project_id in query params

10. [ ] Update all unit tests
    - **Input**: ~18 test files in `services/api/tests/unit/`, `services/langgraph/tests/unit/`, `shared/tests/unit/`, `scripts/tests/`
    - **Output**: Replace `project_id="proj-1"` etc. with valid UUIDs (e.g. `project_id=uuid.UUID("00000000-0000-0000-0000-000000000001")`). Update webhook tests for Repository-based lookup. Remove tests for deleted endpoints/fields.
    - **Test**: `make test-unit` passes

11. [ ] Update scripts, skills, and hardcoded references
    - **Input**: `Makefile` (line 331), `scripts/seed_milestones.py`, `scripts/generate_roadmap.py`, `.claude/skills/triage/SKILL.md`, `.claude/skills/brainstorm/SKILL.md`
    - **Output**: Replace hardcoded `"codegen-orchestrator"` with the project's UUID (from API lookup or env var). Scripts should accept `--project-id` as UUID. Skills should use `$PROJECT_ID` env var or API lookup by name.
    - **Test**: `make backlog` succeeds, skills reference valid project

12. [ ] Integration test — full lifecycle
    - **Input**: All changes from steps 1-11
    - **Output**: Integration test: create project (UUID auto-generated) → create repository with visibility → webhook triggers deploy via Repository lookup → verify cascade delete works
    - **Test**: `make test-integration` passes, `make test-unit` passes

