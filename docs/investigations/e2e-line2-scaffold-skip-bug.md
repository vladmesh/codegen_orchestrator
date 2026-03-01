# E2E Investigation: Line 2 Manual Test — Scaffold Phase Silently Skipped

> **Date**: 2026-03-01
> **Project**: todo_api (project_id: `2939afa5-a5ac-4802-bf49-a7e335379fe3`)
> **Task**: eng-d2f351c5b5d8
> **Status**: Failed. Root cause: race condition in project status — scaffold phase silently skipped.

---

## Timeline

```
01:32:55 — Task created via API (status: draft)
01:33:02 — Engineering worker picks up task
01:33:02 — _create_repo_and_set_secrets() → status set to "scaffolding"
01:33:04 — GitHub repo project-factory-organization/todo-api created
01:33:06 — Secrets set (REGISTRY_URL, REGISTRY_USER, REGISTRY_PASSWORD)
01:33:07 — Resources allocated (vps-267180:8000)
01:33:07 — *** Status overwritten to "developing" BEFORE subgraph runs ***
01:33:08 — developer_node_start — scaffold_config=None (status != "scaffolding")
01:33:08 — Worker spawn requested WITHOUT scaffold_config
01:33:14 — Worker dev-todo-api-fff485be created (empty repo, no scaffold)
01:33:15 — Claude Code starts, builds project from scratch (no template)
01:41:37 — developer_node_success, commit 1e11a20 pushed
01:41:37 — CI gate: looks for ci.yml workflow → 404 (no .github/ in repo)
01:41:39 — Task marked FAILED, worker deleted
```

**Total time**: ~9 min. Claude Code ran ~8 min on empty repo, wasting credits.

---

## Баг 1: Scaffold phase silently skipped (CRITICAL)

### Описание

При action=`create` scaffold phase (copier + make setup + git push) не запускается. Developer node получает пустой репо и Claude Code импровизирует с нуля.

### Корневая причина

**Файл**: `services/langgraph/src/workers/engineering_worker.py`, строки 619-622

```python
# Update project status to developing
await api_client.patch(
    f"projects/{project_id}",
    json={"status": ProjectStatus.DEVELOPING.value},
)
```

Эта строчка выполняется **до** запуска engineering subgraph (строка 625-627). Developer node на строке 227 (`developer.py`) проверяет:

```python
if action != "create" or project_status != "scaffolding":
    return None  # scaffold_config = None → scaffold не запускается
```

Developer node перечитывает project из API (строка 87), видит status=`developing`, думает scaffold уже прошёл.

### Sequence diagram

```
engineering_worker          API              developer_node        worker_manager
      |                      |                     |                     |
      |--- PATCH status →    |                     |                     |
      |    "scaffolding"     |                     |                     |
      |                      |                     |                     |
      |--- PATCH status →    |                     |                     |
      |    "developing"   ←BUG                     |                     |
      |                      |                     |                     |
      |--- run subgraph --→  |                     |                     |
      |                      |--- GET project --→  |                     |
      |                      |    status=developing |                     |
      |                      |                     |                     |
      |                      |    scaffold_config = None (status != scaffolding)
      |                      |                     |                     |
      |                      |                     |--- spawn worker --→ |
      |                      |                     |    (no scaffold)    |
      |                      |                     |                     |
```

### Исправление

Удалить преждевременное обновление статуса на строках 619-622. Статус должен оставаться `scaffolding` пока developer node не завершит scaffold phase. Developer node сам обновляет статус на `scaffolded` после успешного scaffold (developer.py:137).

---

## Баг 2: `config.description` не пробрасывается в TASK.md

### Описание

Project config содержит description (включая audit prompt), но developer node строит TASK.md не из `project.config.description`, а из `EngineeringMessage.description`. При trigger через queue без явного `description` в сообщении — TASK.md получает `**Description**:` пустую.

### Воспроизведение

В TASK.md внутри worker контейнера:
```
**Description**:
```

Хотя project.config.description содержит полный текст задачи + audit instructions.

### Следствие

Audit prompt не доходит до Claude Code. Даже без бага scaffold — описание задачи теряется.

---

## Баг 3: CI gate не обрабатывает отсутствие workflow

### Описание

После успешного push (даже без scaffold), engineering worker пытается проверить CI:

```
GET /repos/.../actions/workflows/ci.yml/runs?branch=main → 404
```

Вместо graceful handling (retry/skip), код бросает `HTTPStatusError` и task падает с uncaught exception.

### Следствие

Даже если бы Claude Code написал рабочий код — task всё равно упал бы из-за отсутствия `.github/workflows/ci.yml`.

### Исправление

`_wait_for_ci_and_fix` должен проверять существование workflow перед polling. Если workflow не существует — fail-fast с понятным сообщением: "ci.yml workflow not found — scaffold likely failed".

---

## Баг 4: `started_at` не обновляется

### Описание

Task в статусе `running`, но `started_at: null`:
```json
{"status": "running", "started_at": null, "updated_at": "2026-03-01T01:33:02.521584Z"}
```

PATCH на строке 500 engineering_worker.py обновляет status на `running` но не ставит `started_at`.

---

## Рекомендации

1. **Баг 1** — критический, блокирует всю Line 2. Убрать строки 619-622 из engineering_worker.py. → ✅ **DONE** (DEVELOPING status moved after `ainvoke()`)
2. **Баг 2** — добавить fallback: если `EngineeringMessage.description` пустой, брать из `project.config.description`. → ✅ **DONE** (fallback в engineering_worker после project fetch)
3. **Баг 3** — в `_wait_for_ci_and_fix` добавить проверку существования workflow перед polling. Fail-fast с понятным error message. → ✅ **DONE** (`except WorkflowNotFoundError` fail-fast)
4. **Баг 4** — minor, добавить `started_at` в PATCH при переходе в `running`. → ✅ **DONE** (`started_at` included in PATCH)
