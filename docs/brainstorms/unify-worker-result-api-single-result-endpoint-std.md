# Worker Result API Unification — Single Endpoint, Binary Outcome

> [!WARNING]
> Этот файл автогенерируется командой `make sync`. Не редактируйте вручную — изменения будут перезаписаны.

# Brainstorm: Worker Result API Unification — Single Endpoint, Binary Outcome

> **Дата**: 2026-03-19
> **Контекст**: Воркер видит два разных API (wrapper localhost:9090 и manager worker-manager:8000), три ручки для результатов (/complete, /failed, /blocker) с неочевидной семантикой выбора между ними. Нужно унифицировать в один фасад с бинарным исходом.
> **Связано с**: bs-1e7245f8 (Developer Blocker + HITL), task-9f294c98 (Task status refactor)
> **Status**: draft

---

## Current State

### Что видит воркер сейчас

**localhost:9090** (wrapper, внутри контейнера):
- `POST /complete` — `{"commit": "sha", "summary": "..."}` → Redis `{status: "completed"}`
- `POST /failed` — `{"reason": "..."}` → Redis `{status: "failed"}`
- `POST /blocker` — `{"reason": "..."}` → Redis `{status: "blocked"}`

**worker-manager:8000** (по сети):
- `POST /api/worker/{id}/infra/compose` — управление compose-инфрой (db, redis)
- `GET /api/introspect/workspaces/{repo_id}/tree` — просмотр файлов workspace
- `GET /api/introspect/workspaces/{repo_id}/files/{path}` — чтение файла

Воркер узнаёт о них из INSTRUCTIONS.md + env vars (`WORKER_MANAGER_URL`, `WORKER_ID`).

### Проблемы

1. **Два адреса** — воркер должен помнить localhost:9090 для результатов и worker-manager:8000 для инфры. Разная ментальная модель.

2. **`/failed` vs `/blocker` — неочевидный выбор**. Воркер (Claude Code) сам решает, какой вызвать. Если вызовет `/failed` когда нужен человек — supervisor бессмысленно ретрайнет. Если `/blocker` когда мог бы просто ретрайнуть — задача зависнет.

3. **Ретрай broken by design**. Supervisor ретраит FAILED задачи автоматом, но если воркер написал "не могу — нет кредов" через `/failed`, ретрай бесполезен. Non-retryable reasons (`worker_rejected`, `developer_blocked`) — хардкод в supervisor, а не свойство результата.

4. **Introspect ручки бесполезны для воркера**. Он сидит в /workspace и может читать файлы напрямую. Introspect нужен admin-панели, а не воркеру.

5. **INSTRUCTIONS.md документирует три ручки** с примерами, но критерии выбора размыты: "failure — when you tried but could not" vs "blocker — when you need human intervention". На практике это часто одно и то же.

---

## Proposal

### Один фасад — wrapper (localhost:9090)

Wrapper становится единственным API для воркера. Все вызовы через localhost:9090.

### Один endpoint для результата — `POST /result`

```json
// Получилось
{
  "success": true,
  "commit": "abc123def",
  "summary": "Implemented feature X, added tests"
}

// Не получилось
{
  "success": false,
  "reason": "Missing S3 credentials — not available in .env, cannot proceed"
}
```

**Бинарный исход**: `success: true` или `success: false`. Третьего не дано.

- `success: true` → требует `commit` (SHA) и `summary`
- `success: false` → требует `reason` (почему не смог)

### Что значит "не получилось"

Любой `success: false` от воркера — это **эскалация до человека**. Автоматический ретрай не делаем. Причины:

1. Воркер уже попробовал. Если бы мог — сделал бы.
2. Если задача нерешаема (нет кредов, инфра сломана, requirements противоречат коду) — ретрай ничего не даст.
3. Если воркер просто не справился с кодом — значит задача слишком сложная или плохо описана. Нужен человек чтобы переформулировать/декомпозировать.

### Когда ретраить автоматом

Ретрай остаётся, но только для **технических сбоев**, которые воркер не контролирует:
- Контейнер убит OOM / timeout (воркер не успел вызвать `/result`)
- Wrapper упал (exception в процессе)
- Сеть отвалилась (Redis publish failed)
- Процесс agent-а вернул non-zero exit code без HTTP-результата

Это всё ловится на уровне wrapper/manager, не на уровне воркера. Статус `FAILED` означает "техническая авария", а не "воркер не справился".

### Infra proxy — тоже через wrapper

```
POST /infra/compose  →  proxy  →  worker-manager:8000/api/worker/{id}/infra/compose
```

Wrapper знает `WORKER_ID` и `WORKER_MANAGER_URL` из env — воркеру не нужно подставлять их в URL.

### Убираем лишнее из воркерского API

- ~~`GET /api/introspect/workspaces/{repo_id}/tree`~~ — воркер в /workspace, `ls` достаточно
- ~~`GET /api/introspect/workspaces/{repo_id}/files/{path}`~~ — `cat` достаточно
- Introspect остаётся в manager для admin-панели

### Итого: API воркера

| Endpoint | Назначение |
|----------|------------|
| `POST /result` | Результат работы (success/fail) |
| `POST /infra/compose` | Управление compose-инфрой |

Два эндпоинта. Один адрес. Минимум когнитивной нагрузки для LLM.

---

## Mapping на стейт-машину задач

| Событие | Task status | Story status | Автоматический ретрай |
|---------|-------------|--------------|----------------------|
| `success: true` | IN_DEV → DONE | (story completion flow) | — |
| `success: false` | IN_DEV → WAITING_HUMAN | IN_PROGRESS → WAITING_HUMAN | Нет |
| Техническая авария (OOM, timeout, crash) | IN_DEV → FAILED | без изменений | Да, если iterations < max |
| `success: true` → CI/QA/deploy fails | откат: DONE → TODO (через reopen flow) | зависит от фазы | Да, новая задача |

### Что меняется в result handler

Сейчас (`engineering_result_handler.py`):
- `handle_engineering_success()` — success path → DONE
- `handle_worker_blocked()` — blocker → WAITING_HUMAN_REVIEW
- `handle_worker_reject()` — reject → FAILED + story FAILED
- `fail_job()` — generic failure → FAILED

После:
- `handle_success()` — `success: true` → DONE
- `handle_worker_gave_up()` — `success: false` → WAITING_HUMAN (бывший blocked + reject, объединены)
- `handle_technical_failure()` — exception/timeout/crash → FAILED (только технические)

### Что меняется в supervisor

Сейчас supervisor различает retryable/non-retryable по `failure_metadata.failure_reason`:
```python
NON_RETRYABLE = {"worker_rejected", "ci_infra_failure", "developer_blocked"}
```

После: supervisor ретраит только `FAILED` (технические аварии). `WAITING_HUMAN` — не трогает вообще. Список `NON_RETRYABLE` не нужен — семантика зашита в статус.

---

## Изменения в INSTRUCTIONS.md

Ключевые изменения для воркера:

### Раздел "Reporting Results" — упростить

**Было**: три curl-а с неочевидным выбором.

**Станет**:
```markdown
## Reporting Results

When done, report via HTTP:

### Task completed
curl -sf -X POST http://localhost:9090/result \
  -H 'Content-Type: application/json' \
  -d '{"success":true,"commit":"<sha>","summary":"<what you did>"}'

### Task not completed
curl -sf -X POST http://localhost:9090/result \
  -H 'Content-Type: application/json' \
  -d '{"success":false,"reason":"<why you cannot complete this task>"}'
```

### Раздел "When to report failure" — чётко прописать

```markdown
## When to report failure (success: false)

Report failure when the task **cannot be completed by writing code**:

**Infrastructure / environment issues:**
- Required API keys, secrets, or credentials missing from .env
- Database unreachable after following troubleshooting steps
- External services/URLs referenced in task are unreachable
- Port conflicts, DNS failures, container runtime issues

**Task definition issues:**
- Requirements contradict each other or the existing codebase
- Task depends on code/APIs that don't exist yet
- The only solution would produce broken or incorrect functionality

**Capability limits:**
- You tried multiple approaches but none produce correct behavior
- The fix requires changes outside your workspace (infrastructure, CI config, other repos)
- The task is too ambiguous to implement without clarification

When in doubt: report failure. A failed task gets escalated to a human who can
fix the root cause, clarify requirements, or decompose the task further.
Shipping broken code wastes more time than escalating.
```

### Раздел "Infrastructure" — убрать WORKER_MANAGER_URL

**Было**: длинные curl-ы с `$WORKER_MANAGER_URL/api/worker/$WORKER_ID/infra/compose`

**Станет**:
```markdown
## Infrastructure

curl -sf -X POST http://localhost:9090/infra/compose \
  -H 'Content-Type: application/json' \
  -d '{"args":["up","-d","--wait","db","redis"],"timeout":120}'
```

Один адрес для всего. Wrapper проксирует в manager.

### Раздел "Step 0: Sanity Check" — адаптировать

Сейчас sanity check вызывает `/failed`. После — вызывает `/result` с `success: false`. Семантика та же: "это не задача для кода, эскалируй".

---

## Scope of changes

### worker-wrapper (packages/worker-wrapper)
- `http_models.py` — новая модель `ResultRequest` вместо трёх; infra proxy model
- `http_server.py` — route `/result` вместо трёх; route `/infra/compose` (proxy)
- `wrapper.py` — обработка нового формата Redis output
- Тесты — обновить под новый API

### engineering result handler (services/langgraph)
- `engineering_result_handler.py` — объединить `handle_worker_blocked` + `handle_worker_reject` → `handle_worker_gave_up`
- `engineering.py` (consumer) — упростить роутинг результатов
- `developer.py` (node) — адаптировать SpawnResult mapping

### worker_spawner (services/langgraph)
- `worker_spawner.py` — `SpawnResult`: убрать `block_reason`/`reject_reason`, добавить `gave_up_reason`

### supervisor (services/scheduler)
- `supervisor.py` — убрать `NON_RETRYABLE` список, ретраить только FAILED

### shared contracts
- `dto/task.py` — убрать `BLOCKED` и `IN_CI`/`TESTING` из TaskStatus (отдельный рефактор, task-9f294c98)

### INSTRUCTIONS.md
- Переписать секции Reporting Results, Infrastructure, Step 0, When You're Stuck

---

## Agent stdout capture + auto-resume

### Проблема

CLI-агенты (Claude Code, Factory) обучены отдавать "финальный ответ" в stdout. Как бы мы ни писали в INSTRUCTIONS.md "дёрни ручку", иногда агент:
- Выплюнет важную инфу в консоль вместо HTTP-вызова
- Закончит работу без вызова `/result`
- Напишет summary в stdout, а ручку забудет

Это уже наблюдалось на практике.

### Текущее поведение

```python
# wrapper.py:167-175
else:
    # Watchdog: agent exited without reporting via HTTP
    logger.warning("agent_exited_without_result")
    error = "Agent exited without reporting result"
    status = "failed"
    await self.redis.publish(output_stream, {"status": "failed", "error": error})
```

stdout/stderr процесса читаются в `execute_agent()`, но:
- При успешном exit code — **отбрасываются** (нигде не сохраняются)
- При failed exit code — попадают в error message
- Никогда не прикрепляются к отчёту/задаче

### Решение: два изменения

#### 1. Сохранять stdout агента

Всегда прикреплять stdout (хвост, ~10KB) к результату. Не для принятия решений, а для аналитики и дебага.

```python
# В wrapper.py, после execute_agent():
agent_stdout = self._last_agent_stdout  # сохранять в execute_agent()

# Прикрепить к результату (любому — success, gave_up, technical failure)
result["agent_stdout_tail"] = agent_stdout[-10_000:] if agent_stdout else None
```

Это попадает в Redis → engineering consumer → можно записать как task event или в run metadata. Позволяет:
- Видеть что агент "думал" перед завершением
- Ловить паттерны "агент написал ответ в консоль вместо ручки"
- Дебажить тихие failures

#### 2. Auto-resume при отсутствии HTTP-результата

Если агент завершился (exit code 0) но не вызвал `/result` — вместо немедленного FAILED, wrapper делает **один resume-attempt**:

```python
# Вместо текущего watchdog:
if not self._result_event.is_set() and exit_code == 0:
    logger.warning("agent_exited_without_result_attempting_resume")
    # Resume с явной инструкцией
    resume_prompt = (
        "You finished without calling the result endpoint. "
        "Call POST http://localhost:9090/result with your result now. "
        "If the task is done: {\"success\": true, \"commit\": \"<sha>\", \"summary\": \"...\"}. "
        "If you could not complete it: {\"success\": false, \"reason\": \"...\"}."
    )
    await self._resume_agent(session_id, resume_prompt)
```

**Один** resume. Если после него всё ещё нет результата → FAILED (техническая авария).

Это работает потому что:
- Claude Code поддерживает `--resume <session_id>` — продолжает с полным контекстом
- Агент уже сделал работу, ему нужно только дёрнуть curl
- HTTP-сервер ещё работает (останавливаем в finally)

#### Почему не парсить stdout

Заманчиво вернуть парсинг stdout ("если агент написал commit SHA в консоль — считаем success"). Но:
- Формат stdout зависит от агента (Claude JSON, Factory text, etc.)
- Парсинг хрупкий — мы от него ушли по хорошей причине
- HTTP endpoint даёт **фидбек** — агент узнаёт принят ли результат (200/400/409)
- Resume даёт агенту шанс **самому** правильно оформить результат

---

## Action Items

- → new task: "Unify worker result API — single `/result` endpoint with binary outcome" (wrapper changes + INSTRUCTIONS.md + result handler refactor)
- → new task: "Add infra compose proxy to wrapper — `POST /infra/compose` via localhost:9090" (wrapper proxy + remove direct manager URL from worker env)
- → backlog task-9f294c98: "Refactor TaskStatus enum" (remove BLOCKED, IN_CI, TESTING — separate from this work but closely related)
- → new task: "Capture agent stdout + auto-resume on missing result" (wrapper: save stdout tail, one --resume attempt before FAILED)
- → idea: "Heartbeat / progress endpoint for long tasks" (wrapper could expose `POST /progress` for intermediate updates — not in scope now but natural extension)

