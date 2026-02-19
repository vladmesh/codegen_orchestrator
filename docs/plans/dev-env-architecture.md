# Plan: Dev Environment Architecture — Iteration 1

> **Дата**: 2026-02-20
> **Ветка**: `dev-env-architecture`
> **Контекст**: Миграция воркеров с host networking + DinD на изолированные bridge-сети + compose-прокси. Полный вертикальный срез: workspace на хосте, dual-network, HTTP API для docker compose, CLI-обёртка, правильный cleanup.
> **Источник**: `docs/brainstorms/dev-env-architecture.md`

---

## Scope

**В итерацию входит:**
- Workspace bind-mount: хостовый `/tmp/codegen/workspaces/<id>/workspace` → `/workspace` в контейнере
- Dual-network: воркер подключён к `codegen_internal` + `dev_proj_<id>`
- Compose proxy: HTTP endpoint `POST /api/worker/{id}/infra/compose` на worker-manager
- CLI команды `orchestrator dev-env compose/start-infra/stop-infra/reset-infra`
- Full cleanup при удалении воркера: compose down, dev network, workspace, Redis keys

**Не входит в итерацию:**
- GC осиротевших ресурсов в scheduler
- Адаптация service-template Makefile
- Удаление DOCKER capability
- Обновление промптов агентов

---

## Реализованные шаги

### ✅ Шаг 1: Network-операции в DockerClientWrapper

**Файл**: `services/worker-manager/src/docker_ops.py`

Добавлены 4 метода:
- `create_network(name, driver="bridge")` — создаёт Docker-сеть
- `remove_network(name)` — удаляет сеть, игнорирует NotFound
- `connect_network(network_name, container_id)` — подключает контейнер к сети
- `disconnect_network(network_name, container_id)` — отключает, игнорирует NotFound

**Тесты**: `services/worker-manager/tests/unit/test_docker_ops.py` — 5 новых тестов в `TestDockerNetworks`

---

### ✅ Шаг 2: Workspace-модуль

**Файл**: `services/worker-manager/src/workspace.py`

Чистые функции:
- `create_workspace(base_path, worker_id)` → `Path`
- `get_workspace_host_path(base_path, worker_id)` → `str`
- `remove_workspace(base_path, worker_id)` — `shutil.rmtree(..., ignore_errors=True)`

**Тесты**: `services/worker-manager/tests/unit/test_workspace.py` — 5 тестов с `tmp_path`

---

### ✅ Шаг 3: Compose Validator

**Файл**: `services/worker-manager/src/compose_validator.py`

- `ALLOWED_COMMANDS = {"up", "down", "build", "run", "ps", "logs", "stop"}`
- `BLOCKED_FLAGS = {"-it", "--interactive", "--tty", "-i", "-t"}`
- `validate_command(args)` — whitelist + blocked flags
- `validate_compose_file(content)` — блокирует ports, absolute volume mounts
- `resolve_compose_path(compose_file, workspace_path)` — проверяет path traversal

**Тесты**: `services/worker-manager/tests/unit/test_compose_validator.py` — 12 тестов

---

### ✅ Шаг 4: Compose Runner

**Файл**: `services/worker-manager/src/compose_runner.py`

- `ComposeRunner(workspace_base_path)`
- `run(worker_id, args, cwd=".", timeout=120, env=None)` → `(exit_code, stdout, stderr)`
- `--project-name worker_<id>` — изоляция имён
- `--project-directory <host_path>` — абсолютный путь на хосте
- Для `up`/`run`/`build` — генерирует `.codegen-network.yml` с override для dev network
- Path traversal protection в cwd
- `HOST_UID=1000`, `HOST_GID=1000` в env

**Тесты**: `services/worker-manager/tests/unit/test_compose_runner.py` — 5 тестов

---

### ✅ Шаг 5: Настройки + Container Config

**`services/worker-manager/src/config.py`** — добавлено:
```python
WORKSPACE_BASE_PATH: str = "/tmp/codegen/workspaces"
INTERNAL_NETWORK: str = "codegen_internal"
WORKER_MANAGER_URL: str = "http://worker-manager:8000"
```

**`services/worker-manager/src/container_config.py`** — добавлено:
- Поле `workspace_host_path: Optional[str] = None`
- В `to_volume_mounts()`: bind-mount `workspace_host_path` → `/workspace`
- В `to_env_vars()`: параметр `worker_manager_url`, пробрасывает `ORCHESTRATOR_WORKER_MANAGER_URL`

**Тесты**: 2 новых теста в `test_container_config.py`

---

### ✅ Шаг 6: WorkerManager — Dual-Network + Workspace + Cleanup

**Файл**: `services/worker-manager/src/manager.py`

`create_worker()`:
1. Создаёт `dev_proj_<id>` сеть
2. Запускает контейнер на `network_name` (= `codegen_internal`)
3. Подключает ко второй сети
4. Сохраняет `worker:meta:<id>` → `{dev_network, workspace_path}`

`create_worker_with_capabilities()`:
1. `workspace.create_workspace(...)` — создаёт workspace на хосте
2. Фиксированный `network_name = settings.INTERNAL_NETWORK`
3. Bridge URLs: `redis://redis:6379`, `http://api:8000`

`delete_worker()`:
1. Получает `dev_network`, `workspace_path` из `worker:meta:*`
2. `compose down -v` — убирает sidecar'ы
3. Удаляет контейнер воркера
4. Удаляет dev network
5. `workspace.remove_workspace(...)` — удаляет workspace на хосте
6. Удаляет Redis keys: `status`, `meta`, `error`, `last_activity`

**Тесты**: 5 новых тестов в `test_manager_logic.py`

---

### ✅ Шаг 7: HTTP Endpoint на Worker-Manager

**`services/worker-manager/src/routers/compose.py`**:
```
POST /api/worker/{worker_id}/infra/compose
```
- Валидирует команду через `validate_command()`
- Валидирует `docker-compose.yml` через `validate_compose_file()`
- Проверяет path traversal в `cwd`
- Запускает через `ComposeRunner`

**`services/worker-manager/src/main.py`**: `ComposeRunner` в `app.state`, роутер подключён

**Тесты**: `tests/service/test_compose_api.py` — 5 тестов

---

### ✅ Шаг 8: CLI-команды `orchestrator dev-env`

**`packages/orchestrator-cli/src/orchestrator_cli/config.py`**:
```python
worker_manager_url: str = Field(..., alias="ORCHESTRATOR_WORKER_MANAGER_URL")
```

**`packages/orchestrator-cli/src/orchestrator_cli/client.py`**:
```python
def get_worker_manager_client() -> httpx.AsyncClient
```

**`packages/orchestrator-cli/src/orchestrator_cli/commands/dev_env.py`**:

| Команда | Описание |
|---------|----------|
| `compose <args...>` | Прямой проброс в compose endpoint |
| `start-infra [services...]` | `compose up -d --wait <services>` |
| `stop-infra` | `compose stop` |
| `reset-infra` | `compose down -v` |

**`packages/orchestrator-cli/src/orchestrator_cli/main.py`**: зарегистрирован `dev-env`

**Тесты**: `packages/orchestrator-cli/tests/unit/test_dev_env.py` — 6 тестов

---

### ✅ Шаг 9: Dockerfile + docker-compose.yml

**`services/worker-manager/Dockerfile`** — установка Docker CLI 26.1.4 + Compose plugin 2.27.1

**`services/worker-manager/pyproject.toml`** — добавлен `pyyaml`

**`docker-compose.yml`**:
- Именованная сеть: `networks.internal.name: codegen_internal`
- worker-manager: `WORKSPACE_BASE_PATH`, `INTERNAL_NETWORK`, `WORKER_MANAGER_URL`; volume `/tmp/codegen/workspaces`; убраны `WORKER_REDIS_URL`/`WORKER_API_URL` (bridge mode)
- redis: убран `ports: "6379:6379"` (воркеры теперь через bridge)

---

### ✅ Шаг 10: Integration тест

**Файл**: `tests/integration/backend/test_dev_env.py`

- `test_workspace_bind_mount` — создаём воркер, тачим файл в /workspace, проверяем на хосте
- `test_dual_network_created` — инспектируем контейнер, проверяем 2 сети
- `test_compose_rejects_ports` — compose с ports → 400
- `test_delete_cleans_everything` — полный цикл cleanup

---

### ✅ Шаг 11: E2E Smoke тест

**Файл**: `tests/e2e/test_dev_env_smoke.py`

Полный вертикальный срез:
1. CreateWorker → wait RUNNING
2. Пишем `docker-compose.yml` (postgres) в workspace
3. `POST /api/worker/{id}/infra/compose {"args": ["up", "-d", "--wait", "db"]}`
4. `exec pg_isready` в воркере — проверяем доступность postgres через dev network
5. DeleteWorker → проверяем: нет контейнеров, нет сети, нет workspace

---

## Результаты

| Категория | Результат |
|-----------|-----------|
| worker-manager unit tests | ✅ 86 passed |
| orchestrator-cli unit tests | ✅ 16 passed |
| Lint (ruff) | ✅ clean |
| Integration tests | написаны, требуют DinD |
| E2E smoke test | написан, требует полного стека |
