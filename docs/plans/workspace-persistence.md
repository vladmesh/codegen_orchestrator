# Plan: Workspace Persistence по project_id

> Brainstorm: [worker-workspace-persistence.md](../brainstorms/worker-workspace-persistence.md)

## Цель

Workspace привязан к `project_id`, а не к `worker_id`. При повторном запуске воркера для того же проекта он подхватывает существующий код. Claude Code ведёт PROGRESS.md для передачи контекста между попытками.

---

## Фаза 1: Протаскивание project_id через стек ✅

> **Статус**: выполнена.
> **Отклонения от плана**: помимо `WorkerConfig.project_id`, `project_id` также добавлен в `context` dict команды (`context={"source": "langgraph", "repo": repo, "project_id": project_id or ""}`). Это даёт дополнительную видимость в логах worker-manager без парсинга config.
> **Тесты**: 4 unit-теста (2 в worker-manager, 2 в langgraph). Все зелёные, существующие не сломаны.

Сейчас `project_id` доступен в DeveloperNode (`state["project_spec"]["id"]`), но не передаётся дальше. Нужно довести его до worker-manager.

### 1.1 Добавить `project_id` в контракт

**Файл**: `shared/contracts/queues/worker.py`

Добавить `project_id` в `WorkerConfig`:

```python
class WorkerConfig(BaseModel):
    name: str
    worker_type: Literal["developer"]
    # ...existing fields...
    project_id: str | None = None  # <-- NEW
```

Optional, чтобы не ломать обратную совместимость (PO-воркеры не имеют project_id).

### 1.2 Передать project_id из DeveloperNode → request_spawn

**Файл**: `services/langgraph/src/clients/worker_spawner.py`

Добавить параметр `project_id` в `request_spawn()`:

```python
async def request_spawn(
    repo: str,
    github_token: str,
    task_content: str,
    task_title: str = "AI generated changes",
    model: str = "claude-sonnet-4-5-20250929",
    agents_content: str | None = None,
    timeout_seconds: int = Timeouts.WORKER_SPAWN,
    project_id: str | None = None,  # <-- NEW
) -> SpawnResult:
```

Прокинуть в `CreateWorkerCommand.config.project_id`.

**Файл**: `services/langgraph/src/nodes/developer.py`

В `run()` (строка ~119): передать `project_id`:

```python
project_id = project_spec.get("id")

worker_result = await request_spawn(
    repo=repo_full_name,
    github_token=access_token,
    task_content=task_message,
    task_title=task_title,
    timeout_seconds=Timeouts.WORKER_SPAWN,
    project_id=project_id,  # <-- NEW
)
```

### 1.3 Consumer: прокинуть в manager

**Файл**: `services/worker-manager/src/consumer.py`

Достать `project_id` из `cmd.config.project_id` и передать в `create_worker_with_capabilities()`.

**Файл**: `services/worker-manager/src/manager.py`

`create_worker_with_capabilities()` — добавить параметр `project_id: str | None = None`.

### Тесты фазы 1

- Unit: `project_id` проходит через все слои (mock в каждом)
- Не ломает существующие тесты (параметр Optional)

---

## Фаза 2: Workspace по project_id ✅

> **Статус**: выполнена.
> **Отклонения от плана**:
> 1. `get_or_create_project_workspace()` — `mkdir(parents=True, exist_ok=True)` вызывается безусловно (идемпотентно), вместо ветвления `if not already_existed`. Проще, результат тот же.
> 2. `_refresh_git_token()` — сигнатура `(self, container_id, repo, token, worker_id)` вместо `(self, container_id, repo_name, github_token)`. Добавлен `worker_id` для structured logging. Использует base64 encoding pattern (как `_setup_git_repo`), а не несуществующий `_exec_in_container`.
> 3. Redis meta — по решению из обсуждения, `project_id` пишется в `create_worker_with_capabilities()` **после** `create_worker()` (а не внутри `create_worker()`), чтобы не прокидывать project_id в низкоуровневый метод. Также добавлен Redis set `workspace:active_projects` для защиты от orphan GC — это не было в оригинальном плане фазы 2, но необходимо для корректной работы существующего GC.
> 4. Orphan GC (`garbage_collect_orphaned_resources`) дополнительно обновлён: читает `workspace:active_projects` и пропускает project workspace directories. Без этого GC удалял бы project workspaces как orphaned (они ведь не привязаны к worker_id).
> 5. `workspace_key` переменная из плана не добавлена — нигде не использовалась.
> 6. Извлечён `_chown_recursive()` хелпер из `create_workspace()` для переиспользования обеими функциями.
> **Тесты**: 9 unit-тестов вместо 5 запланированных (более гранулярное покрытие: workspace routing, token refresh vs clone, Redis meta, active_projects set, delete preservation, orphan GC protection). Все зелёные, существующие не сломаны (108 total в worker-manager).

### 2.1 Изменить workspace path ✅

**Файл**: `services/worker-manager/src/workspace.py`

Добавить функцию для project-scoped workspace:

```python
def get_or_create_project_workspace(base_path: str, project_id: str) -> tuple[Path, bool]:
    """Get or create workspace for a project.

    Returns (workspace_path, already_existed).
    """
    workspace_path = Path(base_path) / project_id / "workspace"
    already_existed = workspace_path.exists()
    if not already_existed:
        workspace_path.mkdir(parents=True, exist_ok=True)
    else:
        # Touch mtime — GC считает возраст с последнего использования
        os.utime(workspace_path)
    # chown в любом случае — новый воркер может иметь другой uid
    _chown_recursive(Path(base_path) / project_id)
    return workspace_path, already_existed
```

### 2.2 create_worker_with_capabilities: использовать project_id для workspace ✅

**Файл**: `services/worker-manager/src/manager.py`

В `create_worker_with_capabilities()` (~строка 422):

```python
# Текущий код:
# ws_path = workspace_mod.create_workspace(settings.WORKSPACE_BASE_PATH, worker_id)

# Новый код:
if project_id:
    ws_path, workspace_existed = workspace_mod.get_or_create_project_workspace(
        settings.WORKSPACE_BASE_PATH, project_id
    )
    workspace_key = project_id
else:
    ws_path = workspace_mod.create_workspace(settings.WORKSPACE_BASE_PATH, worker_id)
    workspace_existed = False
    workspace_key = worker_id

config.workspace_host_path = str(ws_path)
```

### 2.3 Git: clone или refresh token ✅

**Файл**: `services/worker-manager/src/manager.py`

В `create_worker_with_capabilities()` (~строка 465):

```python
if repo_name and github_token and not workspace_existed:
    await self._setup_git_repo(container_id, repo_name, github_token, worker_id)
elif repo_name and github_token and workspace_existed:
    await self._refresh_git_token(container_id, repo_name, github_token)
    logger.info("workspace_reused", project_id=project_id, worker_id=worker_id)
```

### 2.4 `_refresh_git_token()` для resume ✅

**Файл**: `services/worker-manager/src/manager.py`

GitHub App installation token живёт ~1 час. При resume workspace содержит `.git/config` с протухшим токеном. Нужно обновить remote URL:

```python
async def _refresh_git_token(
    self, container_id: str, repo_name: str, github_token: str
) -> None:
    """Update git remote URL with fresh token in existing workspace."""
    remote_url = f"https://x-access-token:{github_token}@github.com/{repo_name}.git"
    await self._exec_in_container(
        container_id, ["git", "remote", "set-url", "origin", remote_url]
    )
    logger.info("git_token_refreshed", repo=repo_name)
```

### 2.5 Redis metadata: хранить project_id ✅

В `create_worker_with_capabilities()` — после `create_worker()`, добавить `project_id` в meta hash + active_projects set:

```python
if project_id:
    await self.redis.hset(f"worker:meta:{worker_id}", "project_id", project_id)
    await self.redis.sadd("workspace:active_projects", project_id)
```

Это нужно для GC (фаза 4) и для связи worker → project.

### 2.6 delete_worker: НЕ удалять workspace если есть project_id ✅

**Файл**: `services/worker-manager/src/manager.py`

В `delete_worker()` (~строка 164):

```python
# Текущий код:
# workspace_mod.remove_workspace(settings.WORKSPACE_BASE_PATH, worker_id)

# Новый код:
project_id = meta.get("project_id") if meta else None
if project_id:
    # Workspace belongs to project, not worker — keep it
    logger.info("workspace_preserved", project_id=project_id, worker_id=worker_id)
else:
    workspace_mod.remove_workspace(settings.WORKSPACE_BASE_PATH, worker_id)
```

### 2.7 Orphan GC: защитить project workspaces ✅

> Добавлено при реализации (не было в оригинальном плане фазы 2).

**Файл**: `services/worker-manager/src/manager.py`

В `garbage_collect_orphaned_resources()`, секция workspace cleanup:

```python
active_projects = await self.redis.smembers("workspace:active_projects")
# ...
for entry in entries:
    if entry not in known_ids and entry not in active_projects:
        # remove...
```

### Тесты фазы 2 ✅

**`tests/unit/test_workspace.py`** (3 теста):
- `test_creates_new_workspace`: новый project_id → (path, False), директория создана
- `test_reuses_existing_workspace`: существующая директория → (path, True)
- `test_chown_recursive_called_in_*`: `_chown_recursive` вызывается в обеих workspace-функциях

**`tests/unit/test_project_id_passthrough.py`** (6 тестов):
- `test_create_worker_uses_project_workspace_when_project_id`: project_id → `get_or_create_project_workspace`, НЕ `create_workspace`
- `test_create_worker_uses_worker_workspace_when_no_project_id`: без project_id → `create_workspace`, НЕ `get_or_create_project_workspace`
- `test_reuse_workspace_calls_refresh_token_not_clone`: workspace_existed=True → `_refresh_git_token`, НЕ `_setup_git_repo`
- `test_new_workspace_calls_clone_not_refresh`: workspace_existed=False → `_setup_git_repo`, НЕ `_refresh_git_token`
- `test_project_id_saved_to_redis_meta`: FakeRedis, проверка `worker:meta:<id>` содержит project_id
- `test_project_id_added_to_active_projects_set`: FakeRedis, проверка `workspace:active_projects` содержит project_id
- `test_delete_worker_preserves_project_workspace`: meta с project_id → `remove_workspace` НЕ вызван
- `test_delete_worker_removes_worker_workspace`: meta без project_id → `remove_workspace` вызван
- `test_orphan_gc_skips_active_project_workspaces`: project_id в active_projects → workspace не удаляется GC

---

## Фаза 3: PROGRESS.md и resume-промпт

### 3.1 Добавить инструкцию про PROGRESS.md в INSTRUCTIONS.md

**Файл**: `services/langgraph/src/prompts/developer_worker/INSTRUCTIONS.md`

Добавить секцию после "## Workflow":

```markdown
## Progress Tracking

Before you start coding, create `/workspace/PROGRESS.md` with your implementation plan.
Update it as you work — check off completed items.

Format:
```
# Progress

## Plan
- [x] Read TASK.md and understand requirements
- [x] Define data models in shared/spec/models.yaml
- [ ] Implement backend controller
- [ ] Add telegram bot handler
- [ ] Write tests
- [ ] Commit and push

## Notes
Any important decisions or blockers.
```

This file helps track progress and enables continuation if the task is interrupted.
```

### 3.2 Два режима TASK.md: initial vs resume

**Файл**: `services/langgraph/src/nodes/developer.py`

DeveloperNode должен знать, resume это или нет. Сигнал: `SpawnResult` или отдельный механизм?

Проще: `request_spawn()` возвращает `workspace_existed` в `SpawnResult`. Но это неправильный уровень — DeveloperNode не должен знать об инфраструктуре.

**Подход**: Worker-manager возвращает `workspace_existed` в ответе на CreateWorkerCommand. Worker-spawner прокидывает это. DeveloperNode не меняется — вместо этого **worker-spawner** выбирает промпт.

Но ещё проще: **INSTRUCTIONS.md** содержит инструкцию для обоих случаев:

```markdown
## Before You Start

Check if `/workspace/PROGRESS.md` exists:
- **If it exists**: A previous developer worked on this but didn't finish.
  Review PROGRESS.md, run `git status`, assess the current state, then continue.
- **If it doesn't exist**: This is a fresh start. Create PROGRESS.md with your plan.
```

Это самый простой подход — не нужно менять промпт, не нужно прокидывать `is_resume`, Claude Code сам определяет по наличию файла.

### Тесты фазы 3

- Тестировать содержимое INSTRUCTIONS.md нет смысла (шаблон)
- Ручная верификация: запустить e2e, убить воркер, проверить что новый видит PROGRESS.md

---

## Фаза 4: GC workspace'ов

### 4.1 Workspace GC по возрасту с последнего использования

**Файл**: `services/worker-manager/src/manager.py`

mtime обновляется при каждом reuse (см. 2.1 `os.utime`), поэтому 24ч считаются от последнего запуска воркера, а не от создания workspace. Проект, который активно ретраят, никогда не попадёт под GC.

Новый метод `garbage_collect_workspaces()`:

```python
async def garbage_collect_workspaces(self, max_age_hours: int = 24) -> None:
    """Remove project workspaces older than max_age_hours with no active workers."""
    # 1. Собрать project_id всех активных воркеров из Redis
    active_projects: set[str] = set()
    async for key in self.redis.scan_iter(match="worker:meta:*"):
        meta = await self.redis.hgetall(key)
        if pid := meta.get("project_id"):
            active_projects.add(pid)

    # 2. Пройтись по workspace директориям
    for entry in os.listdir(settings.WORKSPACE_BASE_PATH):
        if entry in active_projects:
            continue
        ws_dir = Path(settings.WORKSPACE_BASE_PATH) / entry
        age_hours = (time.time() - ws_dir.stat().st_mtime) / 3600
        if age_hours > max_age_hours:
            workspace_mod.remove_workspace(settings.WORKSPACE_BASE_PATH, entry)
            logger.info("workspace_gc_removed", project_id=entry, age_hours=age_hours)
```

### 4.2 Зарегистрировать периодический таск

**Файл**: `services/worker-manager/src/main.py`

```python
# Workspace GC every 6 hours
workspace_gc_task = asyncio.create_task(
    run_periodic_task(
        lambda: worker_manager.garbage_collect_workspaces(max_age_hours=24),
        interval=21600,
        name="workspace_gc",
    )
)
```

### Тесты фазы 4

- `test_workspace_gc_removes_old_workspaces`: workspace без активного воркера + старше 24ч → удаляется
- `test_workspace_gc_preserves_active_workspaces`: workspace с активным воркером → не удаляется
- `test_workspace_gc_preserves_recent_workspaces`: workspace моложе 24ч → не удаляется

---

## Фаза 5: Mutex по project_id

### 5.1 Защита от параллельных воркеров

**Файл**: `services/worker-manager/src/manager.py`

Перед созданием воркера для project_id — проверить что нет другого активного воркера для этого проекта.

```python
async def _check_project_lock(self, project_id: str) -> str | None:
    """Check if another worker is active for this project.
    Returns worker_id if locked, None if free."""
    async for key in self.redis.scan_iter(match="worker:meta:*"):
        meta = await self.redis.hgetall(key)
        if meta.get("project_id") == project_id:
            worker_id = key.split(":")[-1]
            status = await self.redis.hgetall(f"worker:status:{worker_id}")
            if status:  # Worker still tracked
                return worker_id
    return None
```

В `create_worker_with_capabilities()`:

```python
if project_id:
    existing_worker = await self._check_project_lock(project_id)
    if existing_worker:
        logger.warning("project_locked", project_id=project_id, existing_worker=existing_worker)
        # Вернуть ошибку через Redis response
```

### Тесты фазы 5

- `test_project_lock_prevents_second_worker`: два create_worker с одним project_id → второй отклоняется
- `test_project_lock_allows_after_delete`: create → delete → create → OK

---

## Фаза 6: Failure counter + force clean + retry limit

### 6.1 Счётчик consecutive failures в Redis

**Файл**: `services/worker-manager/src/manager.py`

Redis key: `workspace:{project_id}:failure_count`

В `delete_worker()` — после определения статуса воркера:

```python
if project_id:
    failure_key = f"workspace:{project_id}:failure_count"
    if status == "failed":
        await self.redis.incr(failure_key)
    elif status == "completed":
        await self.redis.delete(failure_key)
```

### 6.2 Force clean workspace после 2 consecutive failures

**Файл**: `services/worker-manager/src/manager.py`

В `create_worker_with_capabilities()` — перед созданием workspace:

```python
if project_id:
    failure_key = f"workspace:{project_id}:failure_count"
    failure_count = int(await self.redis.get(failure_key) or 0)

    if failure_count >= 2:
        # Workspace likely in broken state — wipe and start fresh
        workspace_mod.remove_workspace(settings.WORKSPACE_BASE_PATH, project_id)
        logger.warning("workspace_force_cleaned", project_id=project_id, failure_count=failure_count)
```

Логика попыток:
- **Попытка 1** (failure_count=0): fresh workspace, clone repo
- **Попытка 2** (failure_count=1): resume — workspace сохранён, агент продолжает
- **Попытка 3** (failure_count=2): force wipe → fresh workspace, clone repo заново

### 6.3 Reject spawn после 3 consecutive failures

**Файл**: `services/worker-manager/src/manager.py`

В `create_worker_with_capabilities()` — после проверки failure_count:

```python
MAX_RETRIES = 3

if failure_count >= MAX_RETRIES:
    logger.warning(
        "max_retries_exceeded",
        project_id=project_id,
        failure_count=failure_count,
    )
    # Publish error response so engineering-worker doesn't hang
    return CreateWorkerResponse(
        worker_id=None,
        error=f"Max retries ({MAX_RETRIES}) exceeded for project {project_id}",
    )
```

PO получает ошибку через engineering-worker → сообщает юзеру: "не получилось за 3 попытки, нужна помощь".

### 6.4 Сброс счётчика вручную (опционально)

Для ручного вмешательства — redis-cli или будущий API endpoint:
```
DEL workspace:{project_id}:failure_count
```

### Тесты фазы 6

- `test_failure_count_incremented_on_failed`: delete_worker с status=failed → failure_count +1
- `test_failure_count_reset_on_success`: delete_worker с status=completed → failure_count удалён
- `test_force_clean_after_two_failures`: failure_count=2 → workspace удаляется перед созданием
- `test_spawn_rejected_after_three_failures`: failure_count=3 → CreateWorkerResponse с error
- `test_first_attempt_creates_fresh_workspace`: failure_count=0 → обычный clone

---

## Порядок реализации

```
Фаза 1 (контракт + протаскивание)
  └─ Фаза 2 (workspace по project_id + git token refresh)
       ├─ Фаза 3 (PROGRESS.md + resume)   ← можно параллельно
       ├─ Фаза 4 (GC)                      ← можно параллельно
       ├─ Фаза 5 (mutex)                   ← можно параллельно
       └─ Фаза 6 (failure counter + retry limit)  ← можно параллельно
```

Фазы 3-6 независимы друг от друга, зависят только от фазы 2.

---

## Затронутые файлы

| Фаза | Файл | Изменение |
|------|------|-----------|
| 1 | `shared/contracts/queues/worker.py` | `project_id` в `WorkerConfig` |
| 1 | `services/langgraph/src/clients/worker_spawner.py` | параметр `project_id`, прокинуть в команду |
| 1 | `services/langgraph/src/nodes/developer.py` | передать `project_id` в `request_spawn()` |
| 1 | `services/worker-manager/src/consumer.py` | достать `project_id`, передать в manager |
| 1 | `services/worker-manager/src/manager.py` | параметр `project_id` в `create_worker_with_capabilities()` |
| 2 | `services/worker-manager/src/workspace.py` | `get_or_create_project_workspace()` + `os.utime` при reuse |
| 2 | `services/worker-manager/src/manager.py` | workspace по project_id, `_refresh_git_token()`, preserve on delete |
| 3 | `services/langgraph/src/prompts/developer_worker/INSTRUCTIONS.md` | PROGRESS.md инструкция + resume detection |
| 4 | `services/worker-manager/src/manager.py` | `garbage_collect_workspaces()` (mtime с последнего использования) |
| 4 | `services/worker-manager/src/main.py` | периодический таск для workspace GC |
| 5 | `services/worker-manager/src/manager.py` | `_check_project_lock()` |
| 6 | `services/worker-manager/src/manager.py` | failure counter, force clean после 2 failures, reject после 3 |

---

## Решённые вопросы

1. **force_clean_workspace** → **Комбинация: агент + счётчик (фаза 6)**. До порога (2 failures) workspace сохраняется, агент сам разбирается через PROGRESS.md + git status. После 2 consecutive failures — wipe workspace. Worker-manager решает сам по счётчику в Redis, параметр в CreateWorkerCommand не нужен.

2. **Git token freshness** → **`_refresh_git_token()` в worker-manager (фаза 2.4)**. При resume вместо `_setup_git_repo()` вызывается `_refresh_git_token()`, которая делает `git remote set-url origin` с новым токеном. Manager уже получает github_token через CreateWorkerCommand — всё на месте.

3. **Compose sidecar lifetime** → **Не сохранять, воркер перезапустит (вариант A)**. `delete_worker` по-прежнему делает `compose down -v`. Sidecar'ы — эфемерная тестовая инфра, её потеря не критична. Воркер умеет поднимать infra сам, перезапуск занимает секунды. Workspace persistence нужен для кода, не для тестовой инфры.

4. **Workspace GC trigger** → **По mtime с последнего использования (вариант C)**. `get_or_create_project_workspace()` делает `os.utime()` при reuse. GC смотрит mtime — 24ч с последнего запуска воркера, не с создания. Активно ретраимый проект никогда не попадёт под GC.

5. **PO retry limit** → **Минимальная версия включена в фазу 6**. Worker-manager ведёт `workspace:{project_id}:failure_count`. Попытка 1: fresh. Попытка 2: resume. Попытка 3: force wipe + fresh. После 3 failures — reject spawn. PO получает ошибку, сообщает юзеру. Полноценная версия (PO анализирует ошибки, меняет approach) — отдельная задача на потом.
