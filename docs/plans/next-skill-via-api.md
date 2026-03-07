# Plan: `/next` Skill via API (#56)

## Context

Step 1 из [orchestrator-v2-task-management.md](../brainstorms/orchestrator-v2-task-management.md). Первый скилл, мигрирующий с парсинга markdown-файлов на Work Items API.

### Что есть
- `/next` skill (`SKILL.md`) — промпт для Claude Code, парсит `docs/backlog.md` regex'ами, пишет в `docs/STATUS.md`
- Work Items API (из #55): CRUD + action endpoints (`/api/work-items/`)
- 25 work items в БД (14 backlog, 11 done)
- API list endpoint: `GET /api/work-items/?status=backlog` — возвращает items, отсортированные по priority

### Что меняем
- `/next` SKILL.md → вызывает API через `curl` вместо парсинга markdown
- API: добавить `limit` query param на list endpoint (нужен для `?limit=1`)
- STATUS.md по-прежнему обновляется (для контекста агента в `/implement`)
- API-only, без fallback на файлы

### Что НЕ меняем
- `/implement` skill — пока не трогаем (это #57)
- backlog.md — пока остаётся как есть, не генерируется из БД (это #58)
- Никаких изменений в shared/contracts/ или DB schema

## Steps

1. [x] Add `limit` query param to list work items endpoint
   - **Input**: `services/api/src/routers/work_items.py` (line 164, `list_work_items`)
   - **Output**: `list_work_items` accepts `limit: int | None = Query(None)`. When provided, applies `.limit(N)` to query. Default: no limit (backward compat).
   - **Test**: Unit test: `GET /api/work-items/?status=backlog&limit=1` returns max 1 item. `GET /api/work-items/` without limit returns all.

2. [x] Add `sort` query param (optional: by priority or created_at)
   - **Input**: `services/api/src/routers/work_items.py`
   - **Output**: `list_work_items` accepts `sort: str | None = Query(None)`. Values: `priority` (default, current behavior), `created_at`, `-created_at`. Allows skill to get "first by priority" easily.
   - **Test**: Unit test: sort=priority returns items ordered by priority asc; sort=-created_at returns newest first.

3. [x] Rewrite `/next` SKILL.md to use API
   - **Input**: `.claude/skills/next/SKILL.md`, API endpoints
   - **Output**: Updated SKILL.md where:
     - Step 1 "Read current state": still reads `docs/STATUS.md` (via Read tool)
     - Step 2 "Select task": `curl -s http://localhost:8000/api/work-items/?status=backlog&limit=1` instead of parsing backlog.md. If `#ID` argument: `curl -s http://localhost:8000/api/work-items/?limit=50` and find by title prefix `#ID`. Or: add a search endpoint.
     - Step 3 "Check for plan": read plan field from backlog.md (for now — plans not in DB yet)
     - Step 4 "Update STATUS.md": same as before, but populate from API response JSON
     - Step 5 "Start work item": `curl -X POST .../api/work-items/{id}/start` — transitions backlog → in_dev
     - Step 6 "Commit": same as before
   - **Test**: Manual test — run `/next` and verify it picks from API, starts the item, updates STATUS.md.

4. [x] Add `GET /api/work-items/by-tag/{tag}` lookup endpoint
   - **Input**: `services/api/src/routers/work_items.py`
   - **Output**: New endpoint `GET /api/work-items/by-tag/{tag}` where tag is e.g. `53` — finds work item whose title starts with `#53 `. Returns 404 if not found. This lets `/next #53` resolve to the correct work item ID without listing all items.
   - **Test**: Unit test: lookup existing tag returns item, non-existent tag returns 404.

5. [x] Service test: `/next` flow via API
   - **Input**: All previous steps, `services/api/tests/service/`
   - **Output**: `services/api/tests/service/test_next_flow.py` — creates 3 work items (priorities 0, 1, 2), calls `GET ?status=backlog&limit=1`, verifies returns priority=0 item. Then calls `/start`, verifies status=in_dev. Calls `GET ?status=backlog&limit=1` again, verifies returns priority=1 item (first one no longer backlog).
   - **Test**: `make test-api-integration` passes.

6. [x] Cleanup and docs
   - **Input**: All previous steps
   - **Output**: Update brainstorm status. CHANGELOG entry. Verify `make test-unit` and `make lint` pass.
   - **Test**: `make test-unit` green, `make lint` clean.

## Deviations

- Steps 1, 2, 4 combined into a single commit — all are small query param / endpoint additions to the same router file
- Step 3 (SKILL.md) and Step 5 (service test) combined into one commit
- Service test `test_next_start_advances_queue` initially failed in CI due to shared DB state between test files — fixed by removing `limit=1` assertions and filtering by known title prefixes
- Step 2 `sort` param implementation simplified: only `priority` (default), `created_at`, `-created_at` — no generic sort framework
