# Worker Reuse for CI Fix Loop

> **Backlog**: #8
> **Создан**: 2026-02-25
> **Статус**: В разработке

## Context

При CI failure engineering-worker спавнит **новый контейнер** для каждого retry. Это приводит к:

1. **Потеря контекста** — новый agent не знает что пробовал предыдущий, может повторить тот же фикс
2. **Overhead ~30s/retry** — Docker create + agent warmup (Claude Code CLI init, MCP servers, context load)
3. **Расход токенов** — agent заново читает файлы проекта, INSTRUCTIONS.md, анализирует структуру

### Текущий flow

```
_wait_for_ci_and_fix() loop:
  attempt 0: CI fails → _respawn_developer_for_ci_fix()
    → request_spawn() → CreateWorkerCommand → worker-manager
      → create container → git token refresh → inject TASK.md → wrapper.run()
        → agent warmup → git pull → fix → push
    → wait output → DeleteWorkerCommand (implicit, на timeout или в finally)
  attempt 1: CI fails → повторяется всё заново
  attempt 2: CI pass или сдаёмся
```

### Ключевые файлы

| Файл | Роль |
|------|------|
| `services/langgraph/src/workers/engineering_worker.py` | CI fix loop, `_wait_for_ci_and_fix()`, `_respawn_developer_for_ci_fix()` |
| `services/langgraph/src/clients/worker_spawner.py` | `request_spawn()` — одноразовый цикл create → wait → cleanup |
| `packages/worker-wrapper/src/worker_wrapper/wrapper.py` | Agent execution loop, завершается после первого сообщения |
| `services/worker-manager/src/manager.py` | Container lifecycle, workspace reuse, project mutex |
| `services/worker-manager/src/consumer.py` | Command handler (create/delete/status) |
| `shared/contracts/queues/worker.py` | `WorkerCommand`, `WorkerConfig` contracts |

### Что уже работает

- **Workspace persistence**: директория `/tmp/codegen/workspaces/{project_id}` живёт между контейнерами
- **Git token refresh**: при reuse workspace — `git remote set-url` вместо clone
- **Project mutex**: один worker per project через Redis set `workspace:active_projects`
- **Image cache**: все developer workers используют один и тот же Docker image (hash от capabilities)

---

## Целевой flow

```
request_spawn() → CreateWorkerCommand → worker-manager
  → create container → git clone → inject CLAUDE.md + TASK.md → wrapper.run()
    → agent warmup → work → push → publish output
    → wrapper НЕ завершается, ждёт следующий input

_wait_for_ci_and_fix():
  attempt 0: CI fails
    → send_task_to_worker(worker_id, fix_prompt)   ← НЕ создаём новый контейнер
      → XADD worker:{id}:input {prompt: "Fix CI..."}
      → wrapper видит сообщение → agent получает prompt → fix → push → output
  attempt 1: CI fails → repeat
  attempt N: CI pass → send DeleteWorkerCommand

Если agent/container умер:
  → wrapper crash → lifecycle event "failed"
  → fallback: _respawn_developer_for_ci_fix() (старый путь, новый контейнер)
```

---

## Iteration 1: Wrapper multi-turn support

> Wrapper перестаёт завершаться после первого результата и ждёт следующий input.

### 1.1 Wrapper: consume loop вместо one-shot

**File**: `packages/worker-wrapper/src/worker_wrapper/wrapper.py`

Wrapper уже использует `async for message in self.redis.consume(...)` — цикл есть. Но на практике после первого `process_message()` spawner получает output, шлёт `DeleteWorkerCommand`, и контейнер умирает.

**Изменения**:
- Нет изменений в wrapper. Он уже multi-turn по дизайну — `consume()` loop продолжается пока контейнер жив.
- Убедиться что lifecycle event `completed` не триггерит delete.

**Тесты**:
- Юнит-тест: wrapper обрабатывает 2+ сообщения последовательно
- Юнит-тест: после publish output wrapper продолжает слушать input stream

### 1.2 Wrapper: git pull перед каждым turn

**File**: `packages/worker-wrapper/src/worker_wrapper/wrapper.py`

Между turns agent мог пушить код, а затем CI failed. Перед запуском agent в новом turn нужно обновить workspace.

**Изменения** в `process_message()`:
```python
# Before execute_agent, pull latest changes
await self._git_pull()
```

Метод `_git_pull()`:
```python
async def _git_pull(self):
    """Pull latest changes before next agent turn."""
    result = subprocess.run(
        ["/usr/bin/git", "pull", "--rebase=false"],
        cwd=WORKSPACE_DIR,
        capture_output=True, text=True, timeout=60,
    )
    if result.returncode != 0:
        logger.warning("git_pull_failed", stderr=result.stderr)
```

**Тесты**:
- Юнит-тест: `_git_pull()` вызывается перед каждым `execute_agent()`

### 1.3 Wrapper: обновление TASK.md перед каждым turn

**File**: `packages/worker-wrapper/src/worker_wrapper/wrapper.py`

Новый prompt приходит в `data["prompt"]`. Нужно записать его в TASK.md чтобы agent видел актуальное задание.

**Изменения** в `process_message()`:
```python
# Update TASK.md with new prompt (CI fix context)
prompt = data.get("prompt", "")
if prompt:
    self._write_task_md(prompt)
```

**Тесты**:
- Юнит-тест: TASK.md обновляется перед каждым execute_agent

---

## Iteration 2: Spawner multi-turn API

> `request_spawn()` разделяется на create + send_task + wait_output + delete.

### 2.1 Новый API: `send_task_to_worker()`

**File**: `services/langgraph/src/clients/worker_spawner.py`

Новая функция — отправить задачу в существующий worker и дождаться результата:

```python
async def send_task_to_worker(
    worker_id: str,
    task_content: str,
    timeout_seconds: int = Timeouts.WORKER_SPAWN,
) -> SpawnResult:
    """Send a new task to an existing worker and wait for output."""
```

Логика:
1. `XADD worker:{worker_id}:input {prompt: task_content}`
2. Wait for output on `worker:{worker_id}:output`
3. Return `SpawnResult`

Не создаёт контейнер, не шлёт `CreateWorkerCommand`.

**Тесты**:
- Юнит-тест: `send_task_to_worker()` публикует в input stream и ждёт output
- Юнит-тест: timeout возвращает `SpawnResult(success=False)`

### 2.2 Новый API: `delete_worker()`

**File**: `services/langgraph/src/clients/worker_spawner.py`

Явное удаление worker по `worker_id`:

```python
async def delete_worker(worker_id: str) -> None:
    """Send DeleteWorkerCommand for a worker."""
```

Сейчас delete шлётся только при timeout в `request_spawn()`. Нужен явный вызов.

**Тесты**:
- Юнит-тест: `delete_worker()` публикует `DeleteWorkerCommand`

### 2.3 Рефакторинг `request_spawn()`

**File**: `services/langgraph/src/clients/worker_spawner.py`

`request_spawn()` остаётся как есть для обратной совместимости (initial spawn). Но теперь возвращает `worker_id` в `SpawnResult` чтобы caller мог шлёт follow-up задачи.

**Изменение**: `SpawnResult` уже содержит `request_id`. Добавить `worker_id: str | None = None`.

**Тесты**:
- Юнит-тест: `SpawnResult` содержит `worker_id` после успешного spawn

---

## Iteration 3: Engineering worker — reuse вместо respawn

> `_wait_for_ci_and_fix()` использует existing worker для CI fix.

### 3.1 `DeveloperNode` возвращает `worker_id`

**File**: `services/langgraph/src/nodes/developer.py`

Сейчас `DeveloperNode` вызывает `request_spawn()` и возвращает только `SpawnResult`. Нужно прокинуть `worker_id` наверх в engineering worker.

**Изменение**: `request_spawn()` уже будет возвращать `worker_id` (iter 2.3). Прокинуть через return path developer → engineering_worker.

### 3.2 `_wait_for_ci_and_fix()` принимает `worker_id`

**File**: `services/langgraph/src/workers/engineering_worker.py`

Новая сигнатура:
```python
async def _wait_for_ci_and_fix(
    project: dict,
    task_id: str,
    callback_stream: str | None,
    redis: RedisStreamClient,
    developer_started_at: datetime | None = None,
    *,
    user_id: str = "",
    worker_id: str | None = None,   # NEW
) -> tuple[bool, list[dict]]:
```

### 3.3 Замена `_respawn_developer_for_ci_fix` на `send_task_to_worker`

**File**: `services/langgraph/src/workers/engineering_worker.py`

Внутри CI fix loop:
```python
if worker_id:
    # Reuse existing worker
    fix_result = await send_task_to_worker(
        worker_id=worker_id,
        task_content=task_message,
        timeout_seconds=Timeouts.WORKER_SPAWN,
    )
    fix_success = fix_result.success
else:
    # Fallback: spawn new worker (worker died or no worker_id)
    fix_success = await _respawn_developer_for_ci_fix(...)
```

### 3.4 Cleanup: delete worker после CI gate

**File**: `services/langgraph/src/workers/engineering_worker.py`

После выхода из `_wait_for_ci_and_fix()` — удалить worker:
```python
if worker_id:
    await delete_worker(worker_id)
```

### 3.5 Fallback при мёртвом worker

Если `send_task_to_worker()` получает timeout или error — worker мог умереть. Fallback:
```python
fix_result = await send_task_to_worker(worker_id, task_message, ...)
if not fix_result.success and fix_result.error_message == "execution_timeout":
    # Worker likely dead, fall back to respawn
    worker_id = None  # Reset so next iteration uses respawn
    fix_success = await _respawn_developer_for_ci_fix(...)
```

**Тесты**:
- Юнит-тест: CI fix использует `send_task_to_worker` при наличии `worker_id`
- Юнит-тест: fallback на `_respawn_developer_for_ci_fix` при ошибке `send_task_to_worker`
- Юнит-тест: `delete_worker` вызывается после CI gate
- Юнит-тест: `worker_id=None` → старое поведение (полная совместимость)

---

## Iteration 4: Project mutex и lifecycle

> Worker живёт дольше — нужно корректно управлять mutex и cleanup.

### 4.1 Project mutex: hold lock пока worker жив

**File**: `services/worker-manager/src/manager.py`

Сейчас mutex (`workspace:active_projects`) устанавливается при create и снимается при delete. Это уже работает правильно — worker не удаляется между turns, значит mutex держится.

**Проверить**: что delete_worker корректно снимает mutex при финальном удалении.

### 4.2 Lifecycle events: не триггерить delete на `completed`

**File**: `services/worker-manager/src/consumer.py`

Убедиться что lifecycle event `completed` от wrapper (после каждого turn) не триггерит auto-delete worker'а. Сейчас lifecycle events идут в `worker:lifecycle` stream и обрабатываются только worker-manager для мониторинга — delete не триггерится. Проверить что это так.

### 4.3 Timeout safety

Общий таймаут на весь CI fix gate (не per-turn):
- Сейчас: 30 мин per worker × 3 attempts = до 90 мин
- С reuse: можно сократить per-turn timeout, т.к. нет warmup overhead
- Или ввести общий gate timeout (например, 60 мин на весь CI gate)

**Изменения**: Добавить `CI.TOTAL_GATE_TIMEOUT` в `constants.py`. Engineering worker проверяет общий elapsed time.

**Тесты**:
- Юнит-тест: total gate timeout прерывает loop даже если individual turns не timeout'ятся

---

## Verification

```bash
make test-unit              # Все существующие тесты проходят
make test-langgraph-unit    # Тесты engineering_worker, worker_spawner
```

Manual:
1. `make up` — все сервисы стартуют
2. Telegram → PO → create project → engineering
3. Developer пушит код с ошибкой → CI fails
4. Тот же контейнер получает CI fix prompt (логи: `send_task_to_existing_worker`)
5. Agent фиксит → push → CI pass
6. Worker удаляется после CI gate
7. Kill worker mid-CI-fix → fallback на respawn (новый контейнер, логи: `worker_reuse_failed_fallback`)

---

## Risks

| Risk | Mitigation |
|------|------------|
| Claude Code subprocess не поддерживает resume session | Каждый turn = новый subprocess. Warmup сохраняется частично (кеш `.claude/`) |
| Agent OOM / hang между turns | Fallback на respawn. Total gate timeout. |
| TASK.md перезапись ломает agent state | TASK.md обновляется до запуска agent, не во время |
| Stale git state | `git pull` перед каждым turn |
| Project mutex deadlock | Existing cleanup: mutex снимается при delete_worker, GC по таймауту |
