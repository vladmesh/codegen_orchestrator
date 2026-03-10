# Plan: Live Tests + E2E Issue Fixes

> **Source**: `docs/brainstorms/live-tests.md` + `docs/e2e_results/reverse_bot-20260310-2.md`
> **Goal**: TDD-подход — пишем live-тест, проверяем что падает, фиксим, проверяем что проходит.

---

## Предварительные условия

- Стек запущен (`make up`)
- Доступ к обоим репо: `codegen_orchestrator` и `service-template`
- GitHub App credentials в `.env`
- `REGISTRY_USER`, `REGISTRY_PASSWORD`, `ORCHESTRATOR_HOSTNAME` в `.env`

## Инфраструктура тестов

**Перед степами**: создать каркас `tests/live/`.

```
tests/live/
├── conftest.py              # api (httpx), compose_exec, redis, test_project, cleanup
├── test_health.py           # Phase 1: health checks + consumer groups
├── test_api_crud.py         # Phase 1: CRUD operations
├── test_streams.py          # Phase 1: Redis streams
├── test_scaffold.py         # Phase 2: scaffolder env vars (Issue #3)
├── test_scaffold_result.py  # Phase 3: real scaffold → GitHub (Issues #1, #2, #3)
├── test_supervisor.py       # Phase 2: failure retry logic (Issue #4)
├── test_ci_prompt.py        # Phase 2: CI fix prompt content (Issue #6)
├── test_deploy_infra.py     # Phase 5: deploy prerequisites (servers, SSH, ports)
└── test_full_pipeline.py    # Phase 6: MEGA TEST (scaffold → deploy → health check)
```

**Makefile targets:**
```makefile
test-live:                           # Все live-тесты
    pytest tests/live/ -v --tb=short

test-live N=health:                  # Один файл: make test-live N=health
    pytest tests/live/test_$(N).py -v --tb=short
```

**conftest.py** ключевые фикстуры:
- `api_url` — `http://localhost:8000` (или из env `API_URL`)
- `api_client` — `httpx.AsyncClient(base_url=api_url)`
- `redis_client` — подключение к Redis (из env `REDIS_URL`)
- `cleanup_project` — удаление проекта + GitHub repo после теста
- `test_user` — получить/создать тестового пользователя

> **Важно**: тесты работают С ХОСТА, не из контейнера. Это принципиальное отличие от integration-тестов. Проверяем систему как чёрный ящик через публичные порты.

---

## Step 1: test_health.py — Baseline: Health Checks (GREEN)

**Ожидание**: проходит сразу.

**Тесты:**
| Тест | Что проверяем |
|------|---------------|
| `test_api_health` | `GET /health` → 200, `{"status": "ok"}` |
| `test_redis_ping` | Redis `PING` → `PONG` |
| `test_worker_manager_health` | `GET worker-manager:8080/health` → 200 |
| `test_consumer_groups_exist` | Redis: consumer groups для `engineering:queue`, `scaffold:queue`, `deploy:queue` существуют |

**Зависимости**: httpx, redis
**Время**: ~2 сек

**Если падает**: значит стек не полностью поднялся, чиним до продолжения.

---

## Step 2: test_api_crud.py — Baseline: API CRUD (GREEN)

**Ожидание**: проходит сразу.

**Тесты:**
| Тест | Что проверяем |
|------|---------------|
| `test_create_and_get_project` | POST `/projects` → 201, GET → project data |
| `test_create_story_for_project` | POST `/stories` → 201, status=draft |
| `test_create_and_list_tasks` | POST `/tasks` → 201, GET `/tasks?story_id=X` → list |
| `test_task_transitions` | PATCH status: draft → todo → in_dev → done |
| `test_create_user` | POST `/users` → 201 (или GET existing) |

**Cleanup**: удаляем созданные сущности.
**Зависимости**: httpx
**Время**: ~3 сек

---

## Step 3: test_streams.py — Baseline: Redis Streams (GREEN)

**Ожидание**: проходит сразу.

**Тесты:**
| Тест | Что проверяем |
|------|---------------|
| `test_publish_to_stream` | XADD в тестовый стрим → XREAD возвращает сообщение |
| `test_scaffold_queue_exists` | XINFO GROUPS `scaffold:queue` → consumer group есть |
| `test_engineering_queue_exists` | XINFO GROUPS `engineering:queue` → consumer group есть |
| `test_deploy_queue_exists` | XINFO GROUPS `deploy:queue` → consumer group есть |
| `test_po_input_exists` | XINFO GROUPS `po:input` → consumer group есть |

**Не публикуем в реальные очереди** (кроме тестового стрима) — только проверяем что инфраструктура на месте.

**Зависимости**: redis
**Время**: ~1 сек

---

## Step 4: test_scaffold.py — Issue #3: Registry Secrets (RED → GREEN)

**Issue**: После scaffold, GitHub repo не получает `REGISTRY_USER`, `REGISTRY_PASSWORD` secrets. Потому что в `.env` оркестратора эти переменные пустые, или scaffolder не логирует ошибку.

**Root cause** (из кода `services/scaffolder/src/consumer.py:83-106`):
```python
if all([registry_url, registry_user, registry_password]):
    # sets secrets
else:
    log.warning("registry_secrets_skipped", ...)
```
Scaffolder корректно пропускает если переменные пустые и логирует warning. Проблема выше — в `.env`.

**Тесты:**
| Тест | Что проверяем | Ожидание |
|------|---------------|----------|
| `test_scaffold_env_vars_present` | `REGISTRY_USER`, `REGISTRY_PASSWORD`, `ORCHESTRATOR_HOSTNAME` не пустые в scaffolder контейнере | **RED** если `.env` не заполнен |
| `test_scaffold_sets_secrets` | Создаём проект → scaffold:queue → ждём scaffolded → проверяем GitHub secrets через API | **RED** если env пустые |
| `test_scaffold_creates_repo` | После scaffold — repo существует на GitHub, содержит Makefile | GREEN (работало в e2e) |
| `test_scaffold_saves_tree` | После scaffold — project.config.tree не пустой в API | GREEN |

**Фикс:**
1. Убедиться что `.env` содержит валидные `REGISTRY_USER`, `REGISTRY_PASSWORD`, `ORCHESTRATOR_HOSTNAME`
2. Если проблема в том что эти значения не попадают в контейнер — проверить `docker-compose.yml` env mapping

**Проверка secrets через GitHub API:**
```python
# gh api repos/{owner}/{repo}/actions/secrets → names list
# Secrets нельзя прочитать, но можно проверить что они СУЩЕСТВУЮТ
```

**Cleanup**: удалить тестовый GitHub repo после теста.
**Зависимости**: httpx, GitHub App token
**Время**: ~30 сек (scaffold + GitHub API)

---

## Step 5: test_supervisor.py — Issue #4: Infinite Retry Loop (RED → GREEN)

**Issue**: CI gate fails с infra reason → task → `failed` → supervisor retries → dispatcher → worker "nothing to do" → `failed` → loop.

**Root cause** (из кода):
1. CI gate infra failure path (`_ci_gate.py:462-467`) возвращает `(False, ci_attempts, False, None)` — **НЕ reject**
2. Engineering consumer (`engineering.py:584-605`) ставит task `failed` БЕЗ `failure_metadata`
3. Supervisor (`task_dispatcher.py:398-399`) проверяет только `failure_reason == "worker_rejected"` — infra failure не ловит
4. Supervisor retries task → бесконечный цикл

**Ключевое отличие**: `worker_rejected` (worker явно написал REJECTED) vs `ci_gate_failed` после infra rerun. Второй случай не размечен в metadata.

**Тесты:**
| Тест | Что проверяем | Ожидание |
|------|---------------|----------|
| `test_failed_task_with_infra_reason_not_retried` | Создаём task, ставим `failed` + `failure_metadata.failure_reason = "ci_infra_failure"` → запускаем supervise_failed_tasks → task НЕ перешёл в backlog | **RED** — supervisor не проверяет этот reason |
| `test_failed_task_with_worker_rejected_not_retried` | То же с `worker_rejected` → не retried | GREEN (уже работает) |
| `test_failed_task_normal_is_retried` | Task `failed` без special metadata → retried в backlog | GREEN |
| `test_retry_counter_respected` | Task с `current_iteration >= max_iterations` → terminal failure, не retry | GREEN |

**Фикс (два места):**

### A. Engineering consumer — размечать infra failures

`services/langgraph/src/consumers/engineering.py`, после строки 586:

Сейчас `ci_gate_failed` путь не ставит `failure_metadata`. Нужно добавить:
```python
if planning_task_id:
    await api_client.patch(
        f"tasks/{planning_task_id}",
        json={
            "failure_metadata": {
                "failure_reason": "ci_infra_failure",
                "error": fail_msg,
            },
        },
    )
```

Но этого мало — нужно отличить infra failure от code failure. CI gate возвращает `ci_attempts` — последний attempt со статусом `rerun_failed` = infra. Нужно прокинуть эту информацию.

### B. Supervisor — проверять больше failure reasons

`services/scheduler/src/tasks/task_dispatcher.py:398-399`:

```python
# Сейчас:
if task.get("failure_metadata", {}).get("failure_reason") == "worker_rejected":
    continue

# Нужно:
NON_RETRYABLE_REASONS = {"worker_rejected", "ci_infra_failure"}
if task.get("failure_metadata", {}).get("failure_reason") in NON_RETRYABLE_REASONS:
    continue
```

### C. Cleanup story worker on non-retryable failure

Сейчас cleanup происходит только при terminal failure (retries exhausted). Для infra failures тоже нужен cleanup + notification.

**Реализация тестов:**

Это **не чистый live-тест** в смысле "дёрнуть живой стек через HTTP". Supervisor — Python-функция, которая ходит в API. Варианты:

1. **Через API** (предпочтительно): создаём task через API с нужным статусом и metadata → ждём 30 сек (dispatch interval) → проверяем что статус не изменился
2. **Unit-тест**: мокаем `api_client`, вызываем `supervise_failed_tasks()` напрямую

Выбираем вариант 1 (live) как основной + вариант 2 (unit) для быстрого feedback.

**Зависимости**: httpx, время ожидания ~35 сек
**Время**: ~40 сек

---

## Step 6: test_ci_prompt.py — Issue #6: CI Fix Prompt (RED → GREEN)

**Issue**: Промпт говорит "fix, commit, push" но не требует запустить локальные проверки перед пушем. Worker пушит после каждого фикса вместо одного пуша.

**Root cause**: `_build_ci_fix_prompt()` в `_ci_gate.py:64-99` — instruction step 4:
```
4. **If you CAN fix it**: fix the root cause, run local checks, commit and push.
```
"run local checks" слишком расплывчато. Нет явного "run `make lint` and `make test-unit`" и "fix ALL issues before pushing".

**Тест (unit, не live):**
| Тест | Что проверяем | Ожидание |
|------|---------------|----------|
| `test_ci_fix_prompt_requires_local_lint` | Промпт содержит `make lint` | **RED** |
| `test_ci_fix_prompt_requires_single_push` | Промпт содержит инструкцию про один push | **RED** |
| `test_ci_fix_prompt_contains_reject_instructions` | Промпт содержит `## REJECTED` | GREEN |

**Фикс**: обновить шаг 4 промпта:
```
4. **If you CAN fix it**:
   a. Run `make lint` and fix ALL reported issues.
   b. Run `make test-unit` if unit tests exist.
   c. Only after all local checks pass, commit and push ONCE.
   d. Do NOT push after each individual fix — batch all fixes into one push.
```

**Зависимости**: нет (чистый unit-тест, импорт функции)
**Время**: <1 сек

---

## Step 7: Pre-push hook fix — Issue #1 (service-template, RED → GREEN)

**Issue**: Pre-push hook в worker container exits 0 даже когда ни Docker ни ruff не доступны.

**Подход**: этот фикс делается в `service-template`, но верифицируется в оркестраторе через live-тест. После пуша в service-template, copier подтянет новый hook при следующем scaffold.

**Тест** (добавляем в `test_scaffold.py`):
| Тест | Что проверяем | Ожидание |
|------|---------------|----------|
| `test_scaffolded_hook_fails_without_tools` | Клонируем scaffolded repo, симулируем env без Docker/ruff — hook exits non-zero | **RED** |

**Фикс** (в `service-template`):
Файл: `template/.githooks/pre-push`

```bash
# Native fallback
if ! $DOCKER_AVAILABLE; then
    if command -v ruff >/dev/null 2>&1; then
        ruff check .
    elif [ -f ".venv/bin/ruff" ]; then
        .venv/bin/ruff check .
    elif command -v uv >/dev/null 2>&1; then
        uv tool run ruff check .
    else
        echo "ERROR: No lint tools available (docker, ruff, uv). Failing."
        exit 1
    fi
fi
```

**После фикса**: push в service-template → scaffold новый проект → verify hook.

**Зависимости**: git, доступ к scaffolded repo
**Время**: ~20 сек

---

## Step 8: .env.test.jinja fix — Issue #2 (service-template, RED → GREEN)

**Issue**: Сгенерированный `.env.test` не содержит `TELEGRAM_BOT_TOKEN` с валидным форматом → aiogram crashes в CI.

**Тест** (добавляем в `test_scaffold.py`):
| Тест | Что проверяем | Ожидание |
|------|---------------|----------|
| `test_scaffolded_env_test_has_bot_token` | Scaffolded repo содержит `.env.test` с `TELEGRAM_BOT_TOKEN` в формате `\d+:[\w-]+` | **RED** |

**Фикс** (в `service-template`):
Файл: `template/infra/.env.test.jinja` — добавить:
```
TELEGRAM_BOT_TOKEN=123456789:ABCdefGHIjklMNOpqrsTUVwxyz
```

**После фикса**: push в service-template → scaffold → verify `.env.test`.

**Зависимости**: GitHub API для чтения файла из scaffolded repo
**Время**: ~5 сек (проверка файла), ~30 сек (если scaffold)

---

## Порядок выполнения

```
Phase 1: Каркас + Baseline (должны проходить) ✅ DONE
  ├─ Создать tests/live/conftest.py  ✅
  ├─ Создать Makefile targets         ✅ `make test-live` / `make test-live N=health`
  ├─ Step 1: test_health.py          → 7 passed ✅ (API, Redis, worker-manager, 4 consumer groups)
  ├─ Step 2: test_api_crud.py        → 5 passed ✅ (project, story, tasks, transitions, user upsert)
  └─ Step 3: test_streams.py         → 5 passed ✅ (publish/read, 4 queue groups)
  Total: 17 tests, 3.04s

Phase 2: Issue-driven тесты (TDD: RED → fix → GREEN) ✅ DONE
  ├─ Step 4: test_scaffold.py        → 3 passed ✅ (env vars set; deeper secret-on-repo test TBD)
  ├─ Step 5: test_supervisor.py      → 2 RED → fix → 2 GREEN ✅
  │   Finding: `failure_metadata` was a phantom field — no DB column, no schema field.
  │   Fix: migration (JSONB column) + model + TaskUpdate/TaskRead schemas + _to_read() helper
  │   + supervisor NON_RETRYABLE_REASONS = {"worker_rejected", "ci_infra_failure"}
  └─ Step 6: test_ci_prompt.py       → 2 RED → fix → 4 GREEN ✅
      Fix: prompt now requires `make lint`, `make test-unit`, push only once.
  Total: 26 live tests + 9 unit test suites green

Phase 3: Real scaffold tests (test_scaffold_result.py) — ALL GREEN ✅
  ├─ Issue #1: pre-push hook          → GREEN ✅
  ├─ Issue #2: .env.test token        → GREEN ✅
  └─ Issue #3: registry secrets       → GREEN ✅

Phase 4: Fixes + rebuild — ALL DONE ✅
  ├─ Fix pynacl in scaffolder (orchestrator) → rebuild → GREEN ✅
  │   Also: added scaffolder to `make lock-deps` (was missing)
  ├─ Fix scaffolder push: disable hooks before push (make setup re-enables them) → GREEN ✅
  ├─ Fix pre-push hook in service-template (exit 1, not 0) → pushed → GREEN ✅
  ├─ Fix .env.test.jinja in service-template (add TELEGRAM_BOT_TOKEN) → pushed → GREEN ✅
  ├─ Rebuild scheduler (for supervisor NON_RETRYABLE fix) → GREEN ✅
  └─ make test-live → 30/30 ALL GREEN ✅
```

---

## Phase 5: Deploy Infrastructure Tests (`test_deploy_infra.py`)

> **Цель**: Убедиться что вся инфраструктура для deploy на месте, БЕЗ реального деплоя.
> Быстро (< 30 сек), без LLM, без GitHub Actions. Показывает что чинить до mega-теста.

### Предусловия
- Хотя бы один managed-сервер в БД (`is_managed=true`, status in `[active, ready, in_use]`)
- SSH-ключ зашифрован и доступен через API
- Сервер доступен по SSH из langgraph-контейнера

### Тесты

| # | Тест | Что проверяем |
|---|------|---------------|
| 1 | `test_managed_server_exists` | `GET /api/servers?is_managed=true` → минимум 1 сервер со status active/ready/in_use |
| 2 | `test_server_has_ssh_key` | `GET /api/servers/{handle}/ssh-key` → непустой ответ |
| 3 | `test_server_is_reachable_via_ssh` | Из langgraph-контейнера: `ssh -o ConnectTimeout=5 {user}@{ip} whoami` → ok |
| 4 | `test_port_allocation_and_release` | `POST /api/servers/{handle}/ports/allocate-next` → порт, `DELETE /api/allocations/{id}` → cleanup |
| 5 | `test_deploy_workflow_exists_in_scaffold` | Scaffolded repo содержит `.github/workflows/deploy.yml` (проверить через GH API на test-scaffold repo) |
| 6 | `test_deploy_consumer_running` | `deploy:queue` consumer group exists + pending=0 (consumer живой и читает) |

### Возможные фиксы

| Проблема | Фикс |
|----------|------|
| Нет managed-серверов в БД | Добавить через `POST /api/servers/` или seed |
| SSH-ключ отсутствует/невалидный | Обновить через API, проверить Fernet encryption |
| Сервер недоступен по SSH | Firewall, SSH config, ключ не подходит |
| deploy.yml отсутствует в scaffold | Проверить service-template, добавить workflow |
| Port allocation fails | Проверить что servers.handle FK корректен |

### Время: ~15-30 сек

---

## Phase 6: Full Pipeline E2E (`test_full_pipeline.py`) — THE MEGA TEST

> **Цель**: Один тест проходит ВЕСЬ путь от создания проекта до `GET /health` на задеплоенном сервисе.
> Без LLM. Детерминистично. Реальные очереди, реальный GitHub, реальный сервер.

### Маршрут

```
1. API: create project + repo                    (~1s)
2. scaffold:queue → scaffolder                    (~20s)
   └─ copier генерит backend с /v1/health endpoint
3. API: create story + task (обход Architect)      (~1s)
4. Push готовый код в repo (или scaffold уже достаточен)
5. Перевод task → done через API (обход Engineering) (~1s)
6. task_dispatcher: story complete → deploy:queue  (~30s, ждём цикл)
7. deploy worker: allocate server+port             (~5s)
8. deploy worker: GitHub Actions deploy.yml        (~2-5 мин)
9. deploy worker: smoke test                       (~10s)
10. Наш тест: GET http://{server}:{port}/v1/health → 200  (~1s)
```

**Итого: ~3-7 минут.**

### Необходимые изменения (перед написанием теста)

#### 6.1 NoopRunner для worker-wrapper (опционально, для будущего)

Если захотим прогонять через реальный worker pipeline без Claude:

```python
# packages/worker-wrapper/src/worker_wrapper/runners/noop.py
class NoopRunner(AgentRunner):
    """Runner that commits a marker file without calling any LLM."""
    def build_command(self, prompt: str) -> list[str]:
        return [
            "bash", "-c",
            'echo "noop" > .noop-marker && '
            'git add .noop-marker && '
            'git commit -m "chore: noop marker for e2e test" && '
            'git push origin main'
        ]
```

+ Поддержка `agent_type=noop` в wrapper.py (elif chain) и worker-manager image builder.

**НЕ нужен для Phase 6 MVP** — в MVP мы обходим engineering целиком (task → done).
Нужен для Phase 6.1 (через worker pipeline).

#### 6.2 Защита от task_dispatcher подхвата

Проблема: dispatcher каждые 30с подхватывает `todo` задачи и шлёт в `engineering:queue`.
Если мы хотим обойти engineering, нужно чтобы task не висел в `todo`.

**Решение**: создаём task сразу в `backlog`, потом переводим напрямую в `done`:
```
POST /api/tasks/ (status=backlog)
POST /api/tasks/{id}/transition?to_status=todo
POST /api/tasks/{id}/start          → in_dev
POST /api/tasks/{id}/complete       → done
```
Или ещё проще — используем `blocked_by_task_id` как guard, потом снимаем блок
уже при переводе в done.

**Самый простой путь**: создать task, СРАЗУ перевести backlog → todo → in_dev → done
в рамках одного теста, до того как dispatcher успеет подхватить (30с окно).

#### 6.3 Story completion → deploy trigger

После всех tasks в `done`, dispatcher должен:
1. Пометить story как `complete`
2. Опубликовать `DeployMessage` в `deploy:queue`

**Проверить**: dispatcher.complete_stories() ловит нашу story. Условия:
- Story status = `in_progress`
- Все tasks = `done`

Значит story нужно перевести в `in_progress` ДО перевода tasks в done.
Порядок:
```
1. Create story (status=created)
2. Transition story → in_progress
3. Create task (status=backlog)
4. Transition task: backlog → todo → in_dev → done
5. Wait ~30s for dispatcher cycle
6. Verify: story.status == complete, deploy:queue has message
```

#### 6.4 Project status для deploy

Deploy worker проверяет project status. Нужно чтобы project был в состоянии,
допускающем deploy. После scaffold — `scaffolded`. Deploy worker принимает это?

**Проверить**: `process_deploy_job()` — какие project statuses допустимы.

### Тест-план

```python
class TestFullPipeline:
    """THE MEGA TEST: project → scaffold → deploy → health check."""

    @pytest.fixture
    async def pipeline_project(self, api, compose_exec):
        """Full pipeline: scaffold → story → task → deploy."""
        # Setup: create project + repo + scaffold
        # ... (reuse scaffolded_project fixture logic)
        # Then: create story + task, fast-forward to done
        # Wait for deploy
        # Yield: {project, server_ip, port, deployed_url}
        # Cleanup: stop container, delete allocations, delete GH repo, delete DB records

    async def test_deployed_health_endpoint(self, pipeline_project):
        """GET /v1/health on deployed service returns 200."""
        url = pipeline_project["deployed_url"]
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(f"{url}/v1/health")
        assert resp.status_code == 200

    async def test_deploy_created_service_record(self, api, pipeline_project):
        """ServiceDeployment record exists after deploy."""
        resp = await api.get(
            "/api/service-deployments",
            params={"project_id": pipeline_project["project_id"]},
        )
        assert resp.status_code == 200
        deployments = resp.json()
        assert len(deployments) >= 1

    async def test_project_status_active(self, api, pipeline_project):
        """Project status should be 'active' after successful deploy."""
        resp = await api.get(f"/api/projects/{pipeline_project['project_id']}")
        assert resp.json()["status"] == "active"
```

### Cleanup checklist

| Что | Как |
|-----|-----|
| GitHub repo | `gh.delete_repo(org, name)` через langgraph container |
| Port allocations | `DELETE /api/allocations/{id}` |
| Service deployment record | API delete или оставить (не мешает) |
| Docker container on server | SSH: `cd /opt/services/{name} && docker compose down --remove-orphans` |
| Server directory | SSH: `rm -rf /opt/services/{name}` |
| DB records | `DELETE project` (cascade: repos, stories, tasks, runs, events) |
| GitHub secrets | Удалятся вместе с repo |

### Время: ~3-7 минут (основное — GitHub Actions)

### Phase 6.1: Worker Pipeline (future, после NoopRunner)

Тот же маршрут, но вместо ручного `task → done`:
```
4. task_dispatcher → engineering:queue
5. engineering consumer → worker с agent_type=noop
6. noop runner: git commit --allow-empty → push
7. CI gate: ждёт ci.yml (scaffold-код проходит CI)
8. CI success → task done → story complete → deploy:queue
```

Добавляет проверку: engineering consumer, worker-manager, worker lifecycle, CI gate.
Добавляет ~2-5 мин (CI).

---

## Что остаётся за рамками (требует LLM playbooks)

| Issue | Почему нельзя live-тестом |
|-------|--------------------------|
| **#5: PO silent reminders** | PO — LLM agent, нужен real/mock LLM для проверки поведения |
| **#7: CI fix prompt URL** | Можно unit-тестом проверить что URL передаётся, но реальное влияние — только через LLM playbook |

Эти issues перейдут в обновлённые LLM playbooks (фаза 2 из brainstorm).

---

## Риски и соображения

### Связанность scaffold-теста
Step 4 (scaffold) требует: API → Redis → Scaffolder → GitHub. Если что-то из цепочки не работает — тест падает неинформативно. Mitigation: степы 1-3 гарантируют что базовая инфраструктура жива.

### Timing в supervisor-тесте
Step 5 зависит от dispatch interval (30 сек). Тест должен ждать минимум один цикл. Если scheduler не запущен — тест зависнет. Mitigation: таймаут + проверка что scheduler alive (через health или consumer group lag).

### Пересборка после фиксов
Steps 5-6 меняют Python-код в контейнерах. Нужен `make build` или hot-reload (если dev-режим с mount). Проверить какой режим используется.

### Service-template sync
Steps 7-8 требуют push в service-template, затем scaffold нового проекта чтобы подтянуть изменения. Copier использует `--vcs-ref=HEAD` (из MEMORY.md), поэтому нужен commit+push в service-template перед scaffold.
