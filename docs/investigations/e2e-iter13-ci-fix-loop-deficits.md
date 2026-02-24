# E2E Investigation: Iteration 13 — CI Fix Loop Information Deficits

> **Date**: 2026-02-24
> **Project**: reverse-message-bot (project_id: `633a9ff9`)
> **Branch**: dev-env-architecture
> **Status**: Failed (3 attempts exhausted, CI never passed). Root cause fixed in service-template (`ef891c4`).

---

## Timeline

```
03:34:xx — Engineering triggered, scaffolding + developer worker
03:41:30 — Developer finished, commit 1129e84 pushed
03:41:30 — CI check waiting (ci.yml, attempt 0)
03:42:50 — CI FAILED (run 22335606207): PermissionError in backend conftest.py
03:42:51 — Fix worker 1 spawned (7e4851cc) — attempt 1
03:56:02 — Fix worker 1 finished, commit f1a3ca3 pushed (fixed wrong issue — tg_bot imports)
03:56:02 — CI check waiting (attempt 1)
03:57:21 — CI FAILED AGAIN (run 22335914244): same PermissionError in backend conftest.py
03:57:22 — Fix worker 2 spawned (e30907aa) — attempt 2 (last)
~04:10:xx — Fix worker 2 finished (outcome unknown — stack killed)
```

**Total time**: ~30 min, 3 worker containers accumulated, CI never passed.

---

## Первичная ошибка: PermissionError в conftest.py шаблона

### Описание

CI падает при импорте `conftest.py` в backend-сервисе:

```
services/backend/tests/conftest.py:24: in <module>
    TEST_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
E   PermissionError: [Errno 13] Permission denied: '/app/services/backend/tests/.tmp'
```

### Корневая причина

Это баг в `service-template`, а не в сгенерированном коде:

1. **`template/services/backend/tests/conftest.py`** (строка 22-24) делает `mkdir()` на уровне модуля (при импорте), а не в фикстуре
2. **`template/services/backend/Dockerfile`** (строка 44-45) делает `chown -R 1000:1000 /app` и `USER 1000`
3. **`framework/lib/compose_blocks.py`** (строки 99-123) задаёт `user: "${HOST_UID:-1000}"` в test-compose
4. В CI (GitHub Actions) `HOST_UID` не установлен, Dockerfile слои с `COPY` перезаписывают ownership — UID 1000 не может создать директорию внутри `/app`

**Ключевой момент**: девелопер-агент **не может** исправить эту ошибку правильно. Он может только сделать workaround (использовать `/tmp`), но это расходится с шаблоном. Правильный фикс — в `service-template/template/services/backend/tests/conftest.py` или в Dockerfile.

> **FIXED** (`service-template@ef891c4`): conftest.py теперь использует `tempfile.gettempdir() / "backend_tests"` вместо `.tmp/` внутри `/app`. Также обновлён `DATABASE_URL` в `compose_blocks.py` и `compose.framework.yml`.

---

## Дефицит данных у девелопера

### Что девелопер получил

TASK.md для fix-worker:

```markdown
# Task: Fix CI Failures (Attempt 1)

## CI Failure Details

Job 'lint-and-test' failed:
  Step 'Run tests' failed

## Instructions

1. Pull latest changes with `git pull`.
2. Analyze the CI failure details above to understand the root cause.
3. Fix the root cause of the failure.
4. Run any relevant checks locally (linting, tests) to verify your fix.
5. Commit and push your fixes.
```

### Что девелоперу НЕ хватило

1. **Нет стектрейса**. `get_workflow_failure_logs()` возвращает только имена job/step (`Job 'lint-and-test' failed: Step 'Run tests' failed`), а не вывод pytest. Девелопер получил "тесты упали" без информации почему.

2. **Нет логов CI**. Полный лог содержит `PermissionError: [Errno 13] Permission denied: '/app/services/backend/tests/.tmp'` — но девелопер этого не видит. Ему приходится гадать.

3. **Результат**: fix-worker 1 (`7e4851cc`) пофиксил **другую проблему** — добавил `conftest.py` для `tg_bot` с `sys.path` хаком для импорта `generated.clients.backend`. Это мог быть реальный баг, но **не тот**, на котором CI падает. Backend conftest с PermissionError остался нетронутым.

4. **Нет возможности запустить тесты локально**. Worker-контейнер не имеет Docker-in-Docker — девелопер не может запустить `make tests` (который использует docker compose) для верификации фикса.

### Источник: `get_workflow_failure_logs()`

Метод в `shared/clients/github.py` запрашивает `/repos/{owner}/{repo}/actions/runs/{run_id}/jobs` и возвращает только имена упавших job/step. Полные логи job (доступные через `/actions/jobs/{job_id}/logs`) **не запрашиваются и не передаются** в TASK.md.

---

## Дефицит данных у ПО

### Что ПО знает

Ничего. В `po/consumer.py` (строки 154-161):

```python
if event == "progress":
    logger.info("po_progress_event_dropped", user_id=user_id, text=text)
    return
```

Progress-события от engineering-worker **намеренно дропаются** и не доходят до PO-агента. В логах видно: `po_progress_event_dropped text='Waiting for CI checks...'`.

### Что пользователь видит

Между "задача отправлена на разработку" и финальным результатом — **полная тишина** на 5-30 минут. Пользователь не знает:

- Что CI фейлится
- Что идут retry-попытки
- Что девелопер фиксит не ту ошибку
- Что ошибка вообще в шаблоне и девелопер не может её исправить
- Сколько попыток осталось

### Почему так устроено

Дизайн-решение: progress-события не должны засорять LLM-контекст PO. Но в результате PO не может:
- Принять решение о fail fast
- Сообщить пользователю о проблемах
- Эскалировать ошибку шаблона

---

## Накопление контейнеров: отсутствие project_id при CI fix

### Описание

После 3 попыток фикса осталось 3 worker-контейнера:

```
worker-dev-reverse-message-bot-e30907aa   Up 4 minutes
worker-dev-reverse-message-bot-7e4851cc   Up 19 minutes
worker-dev-reverse-message-bot-dec1a295   Up 27 minutes
```

### Корневая причина

`_respawn_developer_for_ci_fix()` не передаёт `project_id` при спавне:

```python
# engineering_worker.py:111-117
worker_result = await request_spawn(
    repo=repo_full_name,
    github_token=access_token,
    task_content=task_message,
    task_title=f"Fix CI failures for {project_name} (attempt {attempt})",
    timeout_seconds=Timeouts.WORKER_SPAWN,
    # ❌ MISSING: project_id=project.get("id")
)
```

Для сравнения, `DeveloperNode.run()` передаёт `project_id`:

```python
# developer.py:120-127
worker_result = await request_spawn(
    ...
    project_id=project_id,  # ✅
)
```

### Последствия

1. **Workspace не переиспользуется**: каждый fix-worker получает новый workspace вместо проектного
2. **Мьютекс не работает**: `_check_project_lock()` не срабатывает — нет `project_id` в мете контейнера
3. **Cleanup не работает**: при удалении worker'а без `project_id` workspace удаляется, а не сохраняется
4. **Контейнеры накапливаются**: старые worker'ы остаются running, потребляют ресурсы и API-токены
5. **Git-контекст теряется**: новый workspace → новый `git clone` → потеря локальных изменений предыдущего fix-worker'а

---

## Итоговая картина: как одна ошибка превращается в каскад

```
Template bug (conftest mkdir at module level)
  → CI fails with PermissionError
    → engineering-worker: "not infra" → respawn developer
      → developer gets no stacktrace, fixes wrong thing
        → CI fails again with same error
          → another respawn, another container, another $5 of API tokens
            → 3rd attempt (last) — same result
              → task marked failed
                → PO tells user "engineering failed" (no details why)
```

Три проблемы усиливают друг друга:
1. **Скудный контекст** → девелопер не может точно диагностировать
2. **Нет классификации** → template-баги ретраятся как code-баги
3. **Тишина для ПО** → ни ПО, ни пользователь не могут вмешаться

---

## Направления решения и статус

### 1. Обогатить контекст для fix-worker

Передавать в TASK.md полные логи упавших job'ов (последние N строк), а не только имена step'ов. `get_workflow_failure_logs()` уже может запросить `/actions/jobs/{job_id}/logs` — данные доступны, просто не используются.

> **Решение**: вместо обогащения `failure_context` на стороне engineering-worker — добавлена подсказка в TASK.md про `gh run list --branch main` и `gh run view <run-id> --log`. Fix-worker уже имеет `gh` CLI (capability `GITHUB_CLI`) и `GH_TOKEN` в env — может сам посмотреть полные логи. Это проще и надёжнее чем парсить/обрезать логи на стороне оркестратора. **DONE**

### 2. Классификация ошибок: code vs template vs infra

Текущая `_is_infra_failure()` ищет маркеры типа "Docker Registry", "SSH". Нужна третья категория — **template/scaffold bugs**: PermissionError в стандартных путях, ошибки в сгенерированных Dockerfile/compose, проблемы с зависимостями из шаблона. На таких ошибках — fail fast, не тратить ретраи.

> **Статус**: открыто. Имеет смысл реализовать когда ПО сможет принимать решение "стоп, это template bug" (нужен tool для остановки engineering).

### 3. Убрать информационный вакуум для ПО

Не все progress-события одинаковые. "Waiting for CI" — можно дропнуть. "CI failed, retrying" — нельзя. Как минимум нужен отдельный тип события (`ci_failure`) который PO consumer не дропает и который позволяет ПО:
- Сообщить пользователю о проблеме
- Принять решение продолжать ретраи или остановиться
- Эскалировать template-баги

> **Решение**: вместо real-time событий (которые жрут LLM-токены без пользы — у ПО нет tool'а остановить retry) — CI attempts записываются в `task_metadata` через API. ПО видит историю попыток при вызове `get_task_status`. Финальные callback-события обогащены: "CI passed after N failed attempt(s)" / "CI failed after N attempt(s), retries exhausted". **DONE**
>
> Изменения: `TaskUpdate` schema принимает `task_metadata`, router мержит (не заменяет) metadata, `_wait_for_ci_and_fix` возвращает `(bool, list[dict])` и пишет `ci_attempts` в task metadata при каждом failure/success.

### 4. Передавать project_id в CI fix spawn

Однострочный фикс в `_respawn_developer_for_ci_fix()`: добавить `project_id=project.get("id")`. Это включит workspace persistence, мьютекс, и правильный cleanup. Предыдущий worker должен быть остановлен перед спавном нового.

> **DONE**: добавлен `project_id=project.get("id")` в `request_spawn` вызов. Покрыто 2 unit-тестами.

### 5. CI-анализатор как отдельный шаг

Между "CI failed" и "respawn developer" добавить шаг анализа: LLM или rule-based система читает полные логи, классифицирует ошибку, решает — ретраить, fail fast, или эскалировать. Это может быть отдельная нода или capability внутри engineering-worker.

> **Статус**: открыто. Связано с пунктом 2 (классификация). Имеет смысл когда появятся повторяющиеся кейсы нефиксируемых ошибок помимо conftest.

---

## Приоритеты

| Проблема | Severity | Усилие | Эффект | Статус |
|----------|----------|--------|--------|--------|
| ~~Нет project_id в CI fix spawn~~ | HIGH | Низкое | Останавливает накопление контейнеров | **DONE** |
| ~~Скудный failure_context~~ | HIGH | Низкое | Девелопер сам смотрит логи через `gh` | **DONE** (подсказка в TASK.md) |
| ~~Progress-события для ПО~~ | MEDIUM | Среднее | ПО видит ci_attempts через get_task_status | **DONE** (task_metadata) |
| Классификация template bugs | MEDIUM | Среднее | Fail fast на нефиксируемых ошибках | Открыто |
| ~~Фикс conftest в service-template~~ | HIGH | Низкое | Устраняет первопричину iter 13 | **DONE** (`ef891c4`) |
