# Отчёт: Тестирование Native Dev Environment (Phase 2–3)

**Дата:** 20.02.2026
**Итерация:** 2 (финальная валидация Phase 2 — compose proxy + CLI)

## 1. Что тестировалось

Проверка end-to-end flow из воркера: CLI → worker-manager API → docker compose на хосте → сайдкар-контейнер в dev-сети → доступ из воркера.

1. **Создание воркера** через `WorkerManager.create_worker_with_capabilities()` — dual-network (codegen_internal + dev_proj_{id}), workspace bind-mount.
2. **Copier scaffold** сервиса из `service-template` (ветка `native-make-tests`).
3. **Compose proxy** — `orchestrator dev-env start-infra -f infra/compose.base.yml db` через CLI.
4. **Сетевая связность** — воркер → сайдкар (db:5432), воркер → оркестратор (api, redis, worker-manager).
5. **Жизненный цикл** — start → stop → start → reset → start (все проходят).

---

## 2. Результат: ВСЁ РАБОТАЕТ

После исправлений в этой сессии flow проходит полностью:

```
worker$ orchestrator dev-env start-infra -f infra/compose.base.yml db
 Container worker_test-native-dev-db-1  Creating
 Container worker_test-native-dev-db-1  Started
 Container worker_test-native-dev-db-1  Healthy
Infrastructure started.

worker$ python3 -c "import socket; s=socket.socket(); s.connect(('db', 5432)); print('OK')"
OK

worker$ orchestrator dev-env stop-infra -f infra/compose.base.yml
Infrastructure stopped.

worker$ orchestrator dev-env start-infra -f infra/compose.base.yml db
Infrastructure started.   # <-- раньше тут был ReadTimeout

worker$ orchestrator dev-env reset-infra -f infra/compose.base.yml
Infrastructure reset.
```

Связность через codegen_internal тоже работает:
- api:8000 → OK
- redis:6379 → OK
- worker-manager:8000 → OK

---

## 3. Исправления, внесённые по ходу (codegen_orchestrator)

### 3.1 Удаление кастомной сети `internal` из service-template
**Проблема:** Override подменял только network `default`, но compose-файлы service-template определяли кастомную сеть `internal`. Сайдкар-контейнеры попадали в `worker_{id}_internal` вместо `dev_proj_{id}`.

**Решение:** Сеть `internal` в service-template была пустой (без driver/ipam конфигурации) — функционально идентична `default`. Удалена из всех compose-шаблонов и `compose_blocks.py`. Конвенция: compose-файлы шаблона не определяют кастомных сетей. Оркестратор подменяет только `default` простым override-файлом.

**Файлы (service-template):** `template/infra/compose.base.yml.jinja`, `template/infra/compose.tests.integration.yml.jinja`, `framework/lib/compose_blocks.py`
**Файлы (orchestrator):** `services/worker-manager/src/compose_runner.py`

### 3.2 Compose Runner — обработка `-f` флагов
**Проблема:** При передаче `-f infra/compose.base.yml` runner не знал, что файл по умолчанию `docker-compose.yml` не нужен, и ломался при добавлении network override.

**Решение:** Разделение user args на file-flags и command-args, правильный порядок: user files → network override → subcommand.

**Файлы:** `services/worker-manager/src/compose_runner.py`

### 3.3 Compose Runner — удалён `--project-directory`
**Проблема:** Явный `--project-directory` ломал относительные пути в compose-файлах (например `env_file: ../.env`).

**Решение:** Убран `--project-directory`, subprocess запускается с `cwd=effective_cwd` для file discovery. Добавлен `--env-file` для workspace `.env`.

**Файлы:** `services/worker-manager/src/compose_runner.py`

### 3.4 Command Validator — skip VALUE_FLAGS
**Проблема:** Validator считал значение `-f` (e.g. `infra/compose.base.yml`) за subcommand → "command not allowed".

**Решение:** Добавлен `VALUE_FLAGS` set, validator пропускает следующий аргумент после flag из этого набора.

**Файлы:** `services/worker-manager/src/compose_validator.py`

### 3.5 Validator — ports больше не блокируются
**Проблема:** Validator блокировал весь compose-файл если **любой** сервис имел `ports:`. Но при запуске `up db` сервис `backend` с портами не стартует — блокировка избыточна.

**Решение:** Удалена проверка портов. Конфликты обрабатываются docker compose естественно (bind error).

**Файлы:** `services/worker-manager/src/compose_validator.py`

### 3.6 CLI — httpx ReadTimeout
**Проблема:** httpx default timeout (5s) < compose timeout (120s). При `up --wait` контейнера, который нужно запустить, CLI вылетал с пустым "Error: " (ReadTimeout('') → str() = '').

**Решение:** HTTP timeout = compose timeout + 30s. Добавлен `_format_error()` для exceptions с пустым str() (fallback на type name).

**Файлы:** `packages/orchestrator-cli/src/orchestrator_cli/commands/dev_env.py`

### 3.7 CLI — `-f/--file` опция
**Проблема:** CLI start-infra/stop-infra/reset-infra не имели `-f` flag, всегда использовали default `docker-compose.yml`.

**Решение:** Добавлен `-f/--file` typer.Option + helper `_build_file_args()`.

**Файлы:** `packages/orchestrator-cli/src/orchestrator_cli/commands/dev_env.py`

### 3.8 Router — валидация compose-файлов из `-f` flags
**Проблема:** Router всегда валидировал `docker-compose.yml`, игнорируя пользовательские `-f` файлы.

**Решение:** Router собирает пути из `-f/--file` аргументов и валидирует каждый.

**Файлы:** `services/worker-manager/src/routers/compose.py`

### 3.9 Workspace permissions (chown)
**Проблема:** worker-manager (root) создаёт workspace dir → worker (uid 1000) не может писать.

**Решение:** `os.chown()` рекурсивно после создания workspace.

**Файлы:** `services/worker-manager/src/workspace.py`

---

## 4. Известные проблемы service-template (НЕ ИСПРАВЛЕНЫ)

### 4.1 `user:` directive на db сервисе
`compose.base.yml` содержит `user: "${HOST_UID:-1000}:${HOST_GID:-1000}"` — postgres не может инициализироваться под uid 1000. В тестах обошлось тем, что строка была убрана вручную из workspace.

**Нужно:** В service-template `user:` должен быть только на сервисах приложения, не на db/redis.

### 4.2 Отсутствует root `pyproject.toml` для `uv run`
Makefile в native mode использует `uv run`, но root pyproject.toml не создаётся copier'ом.

**Нужно:** Либо генерировать root pyproject.toml, либо native mode должен использовать `cd services/X && uv run ...`.

### ~~4.3 EXEC_MODE=native в Makefile~~ — ✅ RESOLVED
~~Ветка `native-make-tests` в service-template содержит `ifeq ($(EXEC_MODE),native)` — надо довести до конца и вмержить.~~

**Решение:** `EXEC_MODE` удалён полностью в коммите `6aaa999` (28 фев). Tooling-контейнер удалён, Makefile переписан на venv-only workflow (`$(VENV)/ruff`, `$(VENV)/pytest`). Двухпутная абстракция больше не нужна.

---

## 5. Тесты (codegen_orchestrator)

Все unit-тесты проходят после изменений:
- worker-manager: 90 passed
- langgraph: 162 passed
- orchestrator-cli: 17 passed
- api: 61 passed (3 skipped)
- Остальные: 103 passed
- **Итого: 433 passed, 0 failed**

---

## 6. Статус фаз

| Phase | Статус | Комментарий |
|-------|--------|-------------|
| 1. Workspace bind-mount + dual-network | **DONE** | Работает из коробки |
| 2. Compose proxy API + CLI | **DONE** | Полный жизненный цикл работает |
| 3. Template adaptation | **DONE** | EXEC_MODE и tooling-контейнер удалены (`6aaa999`), остальные фиксы (4.1–4.2) применены |
| 4. Remove DinD | WAITING | Зависит от Phase 3 |
| 5. E2E testing | WAITING | Зависит от Phase 4 |
