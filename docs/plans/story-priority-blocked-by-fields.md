# Story: priority + blocked_by fields

> [!WARNING]
> Этот файл автогенерируется командой `make sync`. Не редактируйте вручную — изменения будут перезаписаны.

## Context

Add `priority` (int) and `blocked_by_story_id` (FK → stories.id) to the Story model.
Currently Story has: id, project_id, parent_story_id, title, description, acceptance_criteria, status, created_by, timestamps.
The Task model already has priority + sort support — we follow the same patterns.

## Steps

1. [ ] Migration: add priority + blocked_by_story_id columns
   - **Input**: `services/api/migrations/versions/b265e9f78a39_create_stories_table.py` (current head)
   - **Output**: New migration file adding `priority INTEGER DEFAULT 0` and `blocked_by_story_id VARCHAR(255) FK→stories.id` to `stories` table, with index on both columns
   - **Test**: `make migrate` succeeds; rollback works cleanly

2. [ ] Model: add fields to Story SQLAlchemy model
   - **Input**: `shared/models/story.py`
   - **Output**: `priority: Mapped[int]` (default 0) and `blocked_by_story_id: Mapped[str | None]` (FK to stories.id, nullable) added
   - **Test**: Unit test in `test_story_model.py` — verify columns exist with correct types/defaults

3. [ ] Schemas: update Pydantic schemas (create, read, update)
   - **Input**: `services/api/src/schemas/story.py`
   - **Output**: `StoryCreate` gets `priority: int = 0`; `StoryRead` gets `priority: int` + `blocked_by_story_id: str | None`; `StoryUpdate` gets both as optional fields
   - **Test**: Unit test in `test_story_schemas.py` — serialization/deserialization of new fields

4. [ ] Router: filter by priority, sort support, wire new fields in create
   - **Input**: `services/api/src/routers/stories.py`
   - **Output**: `list_stories` gains `priority` filter param and `sort` query param (default: `priority asc, created_at asc`; support `-priority`, `created_at`, `-created_at`). `create_story` passes `priority` and `blocked_by_story_id` to model. `update_story` already handles arbitrary fields via model_dump.
   - **Test**: Unit tests in `test_stories_router.py` — test create with priority, list with sort param, list with priority filter

5. [ ] Validation: cannot start a story if blocked_by story is not completed
   - **Input**: `services/api/src/routers/stories.py` (`start_story` endpoint)
   - **Output**: Before transitioning to IN_PROGRESS, if `story.blocked_by_story_id` is set, fetch the blocking story and assert its status is COMPLETED. Return 422 with clear message if blocked.
   - **Test**: Unit tests — start succeeds when blocker is completed, start fails (422) when blocker is not completed, start succeeds when no blocker set

6. [ ] DTO: add blocked_by validation helper (optional, clean separation)
   - **Input**: `shared/contracts/dto/story.py`
   - **Output**: No DTO changes needed beyond what exists — validation lives in the router since it requires DB access. Skip this step if no pure-logic extraction is warranted.
   - **Test**: N/A — covered by step 5 router tests

