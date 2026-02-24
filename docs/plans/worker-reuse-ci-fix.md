# Worker Reuse for CI Fix Loop

> **Backlog**: #8
> **Создан**: 2026-02-25
> **Статус**: Готово

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

## Iteration 1: Wrapper multi-turn support ✅

> Wrapper перестаёт завершаться после первого результата и ждёт следующий input.

### 1.1 Wrapper: consume loop вместо one-shot ✅

**File**: `packages/worker-wrapper/src/worker_wrapper/wrapper.py`

Wrapper уже использует `async for message in self.redis.consume(...)` — цикл есть. Но на практике после первого `process_message()` spawner получает output, шлёт `DeleteWorkerCommand`, и контейнер умирает.

**Изменения**:
- Нет изменений в wrapper. Он уже multi-turn по дизайну — `consume()` loop продолжается пока контейнер жив.
- Подтверждено: lifecycle event `completed` не триггерит delete (worker-manager consumer обрабатывает только Create/Delete/StatusWorkerCommand).

**Тесты**: `packages/worker-wrapper/tests/unit/test_multi_turn.py`
- ✅ `test_wrapper_processes_multiple_messages` — wrapper обрабатывает 2+ сообщения последовательно
- ✅ `test_wrapper_continues_after_publishing_output` — после publish output wrapper продолжает слушать

### 1.2 Wrapper: git pull перед каждым turn ✅

**File**: `packages/worker-wrapper/src/worker_wrapper/wrapper.py`

**Изменения**: добавлен метод `_git_pull()` и вызов в `process_message()` перед `execute_agent()`. Реализация совпадает с планом.

**Тесты**: `packages/worker-wrapper/tests/unit/test_multi_turn.py`
- ✅ `test_git_pull_called_before_execute_agent`
- ✅ `test_git_pull_called_before_each_turn`
- ✅ `test_git_pull_runs_git_command`
- ✅ `test_git_pull_failure_does_not_crash`

### 1.3 Wrapper: обновление TASK.md перед каждым turn ✅

**File**: `packages/worker-wrapper/src/worker_wrapper/wrapper.py`

**Изменения**: добавлен метод `_write_task_md(prompt)` и вызов в `process_message()`. Добавлена константа `TASK_MD_PATH = "/home/worker/TASK.md"`.

<!-- Отклонение от плана: добавлен `_write_task_md` как отдельный метод (в плане был inline-код).
     Также добавлена обработка OSError с логированием warning, чтобы ошибка записи TASK.md
     не крашила весь wrapper. TASK.md обновляется только при наличии "prompt" в data —
     content-based сообщения (PO workers) не перезаписывают TASK.md. -->

**Тесты**: `packages/worker-wrapper/tests/unit/test_multi_turn.py`
- ✅ `test_task_md_updated_before_execute_agent`
- ✅ `test_task_md_updated_each_turn`
- ✅ `test_write_task_md_writes_file`
- ✅ `test_no_task_md_update_when_no_prompt`

---

## Iteration 2: Spawner multi-turn API ✅

> `request_spawn()` разделяется на create + send_task + wait_output + delete.

### 2.1 Новый API: `send_task_to_worker()` ✅

**File**: `services/langgraph/src/clients/worker_spawner.py`

Реализована функция `send_task_to_worker()` по плану. Переиспользует `_wait_for_response()` для ожидания output.

<!-- Отклонение от плана: send_task_to_worker() также возвращает worker_id в SpawnResult
     (для удобства caller'а), хотя в плане это не было явно указано. -->

**Тесты**: `services/langgraph/tests/unit/test_worker_spawner_multi_turn.py`
- ✅ `test_sends_prompt_to_input_stream_and_waits_output`
- ✅ `test_returns_failure_on_timeout`
- ✅ `test_returns_failure_on_worker_error` (доп. тест — worker отвечает status=failed)

### 2.2 Новый API: `delete_worker()` ✅

**File**: `services/langgraph/src/clients/worker_spawner.py`

Реализована по плану.

**Тесты**: `services/langgraph/tests/unit/test_worker_spawner_multi_turn.py`
- ✅ `test_publishes_delete_command`

### 2.3 Рефакторинг `request_spawn()` ✅

**File**: `services/langgraph/src/clients/worker_spawner.py`

`SpawnResult` получил поле `worker_id: str | None = None`. `request_spawn()` заполняет его из creation response.

**Тесты**: `services/langgraph/tests/unit/test_worker_spawner_multi_turn.py`
- ✅ `test_spawn_result_has_worker_id_field`
- ✅ `test_spawn_result_accepts_worker_id`
- ✅ `test_request_spawn_returns_worker_id`

---

## Iteration 3: Engineering worker — reuse вместо respawn ✅

> `_wait_for_ci_and_fix()` использует existing worker для CI fix.

### 3.1 `DeveloperNode` возвращает `worker_id` ✅

**File**: `services/langgraph/src/nodes/developer.py`

**Изменения**: `DeveloperNode.run()` добавляет `worker_id: worker_result.worker_id` в return dict при success. Поле `worker_id: str | None` добавлено в `EngineeringState` (`services/langgraph/src/subgraphs/engineering.py`).

### 3.2 `_wait_for_ci_and_fix()` принимает `worker_id` ✅

**File**: `services/langgraph/src/workers/engineering_worker.py`

Реализовано по плану.

### 3.3 Замена `_respawn_developer_for_ci_fix` на `send_task_to_worker` ✅

**File**: `services/langgraph/src/workers/engineering_worker.py`

Реализовано по плану. Дополнительно извлечён `_build_ci_fix_prompt()` из `_respawn_developer_for_ci_fix` — оба пути (reuse и respawn) используют один и тот же промпт.

<!-- Отклонение от плана: извлечён _build_ci_fix_prompt() как отдельная функция для
     DRY — и send_task_to_worker, и _respawn_developer_for_ci_fix используют один промпт.
     В плане промпт строился только внутри _respawn. -->

### 3.4 Cleanup: delete worker после CI gate ✅

**File**: `services/langgraph/src/workers/engineering_worker.py`

`_handle_engineering_success()` оборачивает `_wait_for_ci_and_fix()` в `try/finally` и вызывает `delete_worker(worker_id)` в `finally` блоке. Ошибки delete логируются как warning, не крашат процесс.

<!-- Отклонение от плана: delete_worker вызывается в finally (не просто после вызова),
     чтобы worker удалялся и при CI failure, и при success. Ошибки delete перехватываются. -->

### 3.5 Fallback при мёртвом worker ✅

Реализовано по плану. При `execution_timeout` от `send_task_to_worker`:
1. `worker_id = None` (reset для следующих итераций)
2. Fallback на `_respawn_developer_for_ci_fix`
3. При non-timeout failure (agent error, container alive) — `fix_success = False`, worker_id сохраняется

**Тесты**: `services/langgraph/tests/unit/test_engineering_worker_reuse.py`
- ✅ `test_success_includes_worker_id` — DeveloperNode возвращает worker_id
- ✅ `test_reuses_worker_when_worker_id_available` — send_task_to_worker используется
- ✅ `test_without_worker_id_uses_respawn` — backward compatibility
- ✅ `test_delete_worker_called_after_ci_gate` — cleanup
- ✅ `test_no_delete_when_no_worker_id` — нет cleanup без worker_id
- ✅ `test_worker_id_passed_to_ci_fix` — pass-through из _handle_engineering_success
- ✅ `test_fallback_on_send_task_timeout` — fallback при timeout
- ✅ `test_fallback_resets_worker_id_for_next_iteration` — worker_id=None после fallback

---

## Iteration 4: Project mutex и lifecycle ✅

> Worker живёт дольше — нужно корректно управлять mutex и cleanup.

### 4.1 Project mutex: hold lock пока worker жив ✅ (verified)

**File**: `services/worker-manager/src/manager.py`

**Верифицировано**: mutex (`workspace:active_projects`) устанавливается при create (`manager.py:547`: `sadd`) и снимается при delete (`manager.py:169`: `srem`). Worker не удаляется между turns — mutex держится корректно.

### 4.2 Lifecycle events: не триггерить delete на `completed` ✅ (verified)

**File**: `services/worker-manager/src/consumer.py`

**Верифицировано**: `consumer.py:75-84` — `handle_command()` обрабатывает только `CreateWorkerCommand`, `DeleteWorkerCommand`, `StatusWorkerCommand`. Lifecycle events (`completed`) публикуются в `worker:lifecycle` stream и не обрабатываются consumer — auto-delete не триггерится.

### 4.3 Timeout safety ✅

**File**: `services/langgraph/src/config/constants.py`, `services/langgraph/src/workers/engineering_worker.py`

**Изменения**: Добавлен `CI.TOTAL_GATE_TIMEOUT = 3600` (60 мин, env: `CI_TOTAL_GATE_TIMEOUT`). Проверка elapsed time в начале каждой итерации `_wait_for_ci_and_fix`.

<!-- Отклонение от плана: gate_start = datetime.now(UTC) вместо developer_started_at.
     Timeout считает время CI fix loop, а не от начала developer phase —
     у developer уже есть свой timeout (WORKER_SPAWN = 30 мин). -->

**Тесты**: `services/langgraph/tests/unit/test_engineering_worker_reuse.py`
- ✅ `test_total_gate_timeout_aborts_loop` — loop прерывается при TOTAL_GATE_TIMEOUT=0

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
