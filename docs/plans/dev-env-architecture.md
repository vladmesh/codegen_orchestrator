# Plan: Dev Environment Architecture Migration

> **Дата**: 2026-02-19
> **Контекст**: Миграция архитектуры воркеров с Docker-in-Docker на нативную разработку (sidecar-инфраструктура по запросу) для надежности пайплайнов и ускорения работы LLM-агентов. Вариант инфры: flat dev environment с честным `docker compose up` базы на каждый проект (Per-Worker) и нативным запуском остального кода напрямую (tools предустановлены в образе воркера).

---

## Фаза 1: Подготовка инфраструктуры (Workspace Bind-Mount & Изоляция) — ✅ DONE

> **Реализовано**: Iteration 1 (`dev-env-architecture` branch, 2026-02-20)
> **Ветка**: `dev-env-architecture`, коммит `feat: dev-env iteration 1`

Для того чтобы оркестратор (находящийся на хосте) мог корректно вызывать `docker compose` для проектов агента, файлы пространства имен воркера должны быть доступны на хосте.

1. **Per-Worker директории на хосте**:
   - Обновить логику `worker_manager.create_worker` (`services/worker-manager/src/manager.py`).
   - Создавать директорию `/tmp/codegen/workspaces/<worker_id>/workspace/` на хосте.
   - Монтировать (bind-mount) эту директорию как `/workspace` в контейнер воркера.
   - Клонирование репозитория продолжает происходить **внутри контейнера** через существующий `auto_setup_git_repository()` — файлы попадают на хост автоматически через bind-mount.

2. **Миграция с Host Networking на Dual-Network**:

   **Текущее состояние**: Воркеры запускаются в `network_mode: "host"` (настройка по умолчанию в `manager.py`). Доступ к API и Redis — через `localhost:8000` / `localhost:6379`.

   **Целевое состояние**: Каждый воркер подключён к **двум bridge-сетям**:
   - `internal` (существующая сеть compose-стека оркестратора) — доступ к API (`http://api:8000`) и Redis (`redis://redis:6379`).
   - `dev_proj_<worker_id>` (новая изолированная сеть) — сюда попадают sidecar-сервисы проекта (db, redis и т.д.), имена вроде `db:5432` работают без конфликтов между воркерами.

   **Необходимые изменения**:
   - В `manager.py`: заменить `network_mode: "host"` на подключение к двум именованным сетям.
   - Создавать сеть `dev_proj_<worker_id>` при создании воркера, подключать контейнер воркера к обеим сетям.
   - Обновить `WORKER_API_URL` с `http://localhost:8000` → `http://api:8000`.
   - Обновить `WORKER_REDIS_URL` с `redis://localhost:6379` → `redis://redis:6379`.
   - Убедиться, что Redis и API доступны из `internal` сети (уже работает в docker-compose).

3. **Жизненный цикл данных sidecar'ов**:
   - Volumes **персистентны на время жизни воркера**. `stop-infra` останавливает контейнеры, но **не удаляет volumes** (используется `docker compose stop`, не `down -v`). Это позволяет агенту поднять БД, накатить миграции, и при повторном `start-infra` данные сохраняются.
   - Агент может явно очистить данные через `orchestrator dev-env compose down -v`, если хочет начать с чистого состояния.
   - Полная очистка происходит только при `delete_worker`.

4. **Автоматический и Фоновый Cleanup (Garbage Collector)**:
   - ✅ Обновить логику `worker_manager.delete_worker`:
     - Останавливать инфраструктуру проекта через `docker compose down -v` (с удалением volumes).
     - Удалять сеть `dev_proj_<worker_id>`.
     - Удалять директорию `/tmp/codegen/workspaces/<worker_id>/`.
   - ⏳ **TODO**: В сервисе `scheduler` добавить фоновую джобу для периодической очистки "осиротевших" сетей `dev_proj_*`, volumes `worker_*` и воркспейсов на диске (на случай OOM или хард-рестарта оркестратора). *Не реализовано в итерации 1 — `delete_worker` чистит за собой, но при крэше оркестратора ресурсы осиротеют.*

---

## Фаза 2: API и CLI для управления инфраструктурой — ✅ DONE

> **Реализовано**: Iteration 1 (`dev-env-architecture` branch, 2026-02-20)

Оркестратор должен предоставлять агенту возможность управлять Docker через API + CLI-обёртку, так как у самого агента прав на запуск Docker не будет.

> **Примечание**: API-эндпоинт (Фаза 2) и CLI-обёртка (ранее Фаза 4) объединены, так как эндпоинт бесполезен без клиента, а CLI — без бэкенда.

1. **API Endpoint: `POST /api/worker/{worker_id}/infra/compose`**:
   - Единый эндпоинт для проксирования команд `docker compose`.
   - Принимает команду (например: `["up", "-d", "db", "redis"]` или `["-f", "infra/compose.tests.integration.yml", "run", "integration-tests"]`).
   - **Whitelist команд**: Разрешены только `up`, `down`, `build`, `run`, `ps`, `logs` (без интерактивного режима `-it` / stdin). Остальные — отклоняются с 400.
   - **Валидация путей и безопасности Compose-файлов**:
     - Compose-файлы (`-f`) резолвятся относительно workspace и не могут выходить за его пределы (защита от path traversal).
     - **Анализ манифеста**: API жестко блокирует запуск, если в compose-файле найдены маунты абсолютных путей (защита от Filesystem Escape вида `/:/host_root`). Разрешены только относительные `./` и именованные volumes.
     - **Порты**: Не блокируются на уровне валидатора — конфликты обрабатываются docker compose естественно (bind error). Фаза 4 запретит агентам добавлять `ports` через промпты.
   - **Трансляция путей**: Worker-manager запускает `docker compose` с:
     - `--project-name` = `worker_<worker_id>` (изоляция имён контейнеров между воркерами).
     - `--env-file` = `/tmp/codegen/workspaces/<worker_id>/workspace/.env` (если существует).
     - Subprocess запускается с `cwd=<workspace>/<cwd>` для auto-discovery compose-файлов.
   - **Подключение к сети**: Compose runner инжектирует override-файл (`.codegen-network.yml`), который перенаправляет `default` сеть на `dev_proj_<worker_id>` (external). Конвенция: compose-файлы из service-template **не определяют кастомных сетей**, все сервисы попадают в `default`.
   - **Порты**: Sidecar-сервисы **не публикуют порты** на хост (конвенция, enforced через промпты). Доступ только по имени сервиса через общую сеть `dev_proj_<worker_id>`.
   - **Сценарий Persistent sidecars (`up -d`)**: Worker-manager выполняет `docker compose up --wait` с timeout (по умолчанию 60s) и возвращает статус об успехе. Магии с генерацией Connection Strings нет: агент сам управляет `.env` файлом и устанавливает доступы, обращаясь к сервисам по хостнеймам. Compose-файлы проекта должны содержать `healthcheck` для корректной работы `--wait`.
   - **Решение проблемы прав файлов**: Worker-manager передаёт в `docker compose` переменные `HOST_UID` и `HOST_GID` (совпадающие с UID/GID агента внутри контейнера, обычно 1000:1000), чтобы генерируемые Docker'ом файлы не становились `root`-owned.

2. **CLI-обёртка (`orchestrator-cli`)**:
   - `orchestrator dev-env compose [...]` — прямой проброс в API-эндпоинт.
   - `orchestrator dev-env start-infra [services...]` — сахар: `compose up -d --wait <services>` (ожидает запуска и healthcheck'ов баз).
   - `orchestrator dev-env stop-infra` — сахар: `compose stop` (останавливает контейнеры, volumes сохраняются).
   - `orchestrator dev-env reset-infra` — сахар: `compose down -v` (полная очистка данных, агент вызывает явно).

3. **Граница нативного vs Docker-выполнения**:
   - `docker compose` (через `orchestrator dev-env`) используется **только** для:
     - Поднятия инфраструктурных sidecar-зависимостей (`start-infra db redis`).
     - Запуска интеграционных тестов, завязанных на compose-оркестрацию (`compose -f infra/compose.tests.integration.yml run integration-tests`).
     - Сборки Dockerfile'ов (`compose build`).
   - Нативно (внутри контейнера воркера через `make EXEC_MODE=native`) выполняются:
     - Линтеры (`ruff`, `xenon`).
     - Юнит-тесты (`pytest tests/unit`).
     - Кодогенерация (`framework generate`, `sync_services`).
   - Все необходимые инструменты (ruff, xenon, pytest, mypy, PyYAML, jinja2 и др.) предустановлены в образе `worker-base-common`. Docker-слои шарятся между всеми воркерами (не дублируются ×N).

---

## Фаза 3: Адаптация Шаблонов (`service_template`) — ✅ DONE

> **Реализовано**: 2026-02-20, коммиты `b6c2685`, `173db20` в `service-template/main`
> **Образ воркера**: `worker-base-common/Dockerfile` обновлён в `codegen_orchestrator/dev-env-architecture`

Шаблоны должны быть готовы к нативному выполнению большей части операций. Интеграционные тесты остаются в Docker Compose.

1. **Удаление кастомной сети `internal` из compose-файлов** — ✅ DONE:
   - Конвенция: compose-файлы шаблона **не определяют кастомных сетей**. Все сервисы попадают в `default` (стандартное поведение docker compose).
   - Это позволяет оркестратору подменять только `default` → `dev_proj_<worker_id>` одним простым override-файлом, без парсинга compose YAML.
   - Удалено из: `compose.base.yml.jinja`, `compose.tests.integration.yml.jinja`, `compose_blocks.py` (все шаблоны сервисов).

2. **Рефакторинг `Makefile` — прямые вызовы вместо Docker** — ✅ DONE:
   - Введена переменная `EXEC_MODE ?= docker` (по умолчанию — Docker для обратной совместимости, воркеры выставляют `EXEC_MODE=native`).
   - **Решение**: Прямые вызовы инструментов с `PYTHONPATH=.framework` вместо `uv run`.
   - **Причина отказа от `uv run`**: uv создаёт изолированный `.venv` и не может переиспользовать пакеты, установленные в системном site-packages образа. При этом `poetry`-формат `pyproject.toml` сервисов (без `[project]` таблицы) несовместим с `[tool.uv.workspace]` — uv падает с `No 'project' table found`.
   - **Реализация** в `template/Makefile.jinja`:
     ```makefile
     ifeq ($(EXEC_MODE),native)
     RUN_TOOLING := PYTHONPATH=.framework
     PYTHON_TOOLING := PYTHONPATH=.framework python3
     else
     RUN_TOOLING := $(COMPOSE_ENV_TOOLING) $(DOCKER_COMPOSE) $(COMPOSE_TEST_UNIT) run --build --rm tooling
     PYTHON_TOOLING := $(RUN_TOOLING) python
     endif
     ```
   - В native-режиме все инструменты (ruff, pytest, xenon, mypy, framework) вызываются напрямую из системного Python, а `PYTHONPATH=.framework` обеспечивает доступ к модулям фреймворка.
   - Цель `tooling-tests` также адаптирована: `PYTHONPATH=.framework pytest -q ...` в native-режиме.
   - Интеграционные тесты по-прежнему запускаются через Docker Compose (`make tests` с compose up/run).

3. **Совместимость кодогенерации и безопасности** — ✅ DONE:

   **a) Права файлов — `user:` в compose + `chown`/`USER` в Dockerfile:**
   - В `framework/lib/compose_blocks.py` добавлена директива `user: "${HOST_UID:-1000}:${HOST_GID:-1000}"` ко всем 5 шаблонам сервисов (backend, frontend, tg_bot, notifications_worker, faststream).
   - Из `compose.base.yml.jinja` директива `user:` убрана для `db` (postgres) — postgres не может работать под uid 1000 (ему нужен свой пользователь для инициализации).
   - **Критический момент**: Статические `template/services/*/Dockerfile` **перезаписываются** командой `sync_services create` из Jinja2-шаблонов `framework/templates/docker/*.Dockerfile.j2`. Поэтому `chown`/`USER` добавлены именно в шаблоны фреймворка:
     - `framework/templates/docker/python-fastapi.Dockerfile.j2` — `RUN chown -R 1000:1000 /app` + `USER 1000`
     - `framework/templates/docker/python-faststream.Dockerfile.j2` — аналогично
     - `framework/templates/docker/node.Dockerfile.j2` — аналогично
   - Синхронизировано в `template/.framework/` через `scripts/sync-framework-to-template.sh`.

   **b) `pyproject.toml` — убран uv workspace:**
   - Из `template/pyproject.toml.jinja` удалена секция `[tool.uv.workspace]` с `members = ["services/*"]`.
   - Причина: poetry-формат `pyproject.toml` сервисов несовместим с uv workspace resolution.
   - Файл теперь содержит только минимальный `[project]` с метаданными.

   **c) `ruff.toml` — добавлен exclude `.venv`:**
   - В `template/ruff.toml` добавлен `.venv/**` в список `exclude`.
   - Причина: если `uv` или другой инструмент создаёт `.venv`, ruff не должен линтить файлы внутри.

   **d) Healthcheck'и**: Уже присутствуют в compose-файлах шаблона для инфраструктурных сервисов (db, redis). `--wait` корректно работает.

4. **Tooling в образе воркера (`worker-base-common`)** — ✅ DONE:

   Для нативного выполнения make-целей воркеру нужны все инструменты, которые раньше жили в tooling-контейнере.

   **Решение**: Установка инструментов прямо в `worker-base-common` Docker-образ.

   **Обоснование выбора**:
   - Docker layer sharing: все N воркеров используют одни и те же слои образа — пакеты существуют на диске **один раз**, а не ×N.
   - Слой с тулингом размещён **до** volatile-слоя с wheels (shared/packages) — изменения кода не инвалидируют кэш тулинга.
   - Тулинг-слой пересобирается только при обновлении версий (раз в месяц), не при каждом изменении кода оркестратора.

   **Изменения в `worker-base-common/Dockerfile`**:
   - Добавлен `make` в apt-get install (нужен для `make lint`, `make format` и т.д.).
   - Добавлен стабильный слой с pip-пакетами (до COPY wheels):
     ```dockerfile
     RUN pip install --no-cache-dir \
         ruff==0.14.5 \
         copier==9.4.1 \
         xenon==0.9.1 \
         pytest==8.2.0 \
         pytest-cov==4.1.0 \
         mypy==1.10.0 \
         types-PyYAML \
         PyYAML \
         jinja2
     ```
   - Volatile wheels (shared, worker-wrapper, orchestrator-cli) устанавливаются **после** тулинга.

   **E2E проверка** (воркер `test-native-v3`):
   - `make format` — ✅ ruff format отработал, файлы отформатированы
   - `make sync-services check` — ✅ "Everything is in sync"
   - `make generate-from-spec` — ✅ без ошибок
   - `make lint` — ✅ ruff check + xenon + validate_specs + enforce_spec_compliance + lint_controllers
   - Compose proxy lifecycle — ✅ `start-infra db` → connectivity → `reset-infra`

---

## Фаза 4: Удаление Docker-in-Docker и обновление промптов — ✅ DONE

> **Реализовано**: 2026-02-20, ветка `dev-env-architecture`

Изменение рабочего окружения агента внутри контейнера. Эту фазу можно активировать только после завершения Фаз 2 и 3.

1. **Удаление Docker-in-Docker (Hard Boundary)**:
   - В `worker-manager/src/container_config.py`: убрать маунт `/var/run/docker.sock` из конфигурации capability `DOCKER`.
   - В `shared/contracts/queues/worker.py`: удалить `DOCKER` из enum `WorkerCapability`.
   - В `worker-manager/src/image_builder.py`: удалить Docker CLI/Compose из capability install map.
   - Обновить все `agent_configs` в БД, убрав `DOCKER` из списка capabilities (миграция или seed-скрипт).

2. **Обновление системных промптов (INSTRUCTIONS.md)**:
   - Запретить агентам вызывать `docker` или `docker compose` напрямую.
   - Инструктировать использовать `orchestrator dev-env start-infra db redis` для работы с персистентными sidecar-сервисами, сохраняя доступы в `.env` файл (через обращения к хостнеймам `db:5432` без публикаций портов `ports`).
   - Явно запретить добавлять директиву `ports` в свои `docker-compose.yml`, так как публикация портов на хосте будет конфликтовать между изолированными агентами.
   - Инструктировать использовать `orchestrator dev-env compose` для интеграционных тестов.
   - Указать, что линтеры, генерация и юнит-тесты должны запускаться нативно через `make` с `EXEC_MODE=native`.

---

## Фаза 5: E2E Тестирование нового пайплайна

1. **Сценарии проверки**:
   - Агент scaffold'ит проект → `start-infra db` → миграции → `make test-unit` → push. Проверить полный цикл.
   - Агент запускает `compose -f ... run integration-tests`. Проверить, что тесты проходят через API-прокси.
   - Параллельная работа 2+ воркеров: нет конфликтов портов, сетей, имён контейнеров.
   - Cleanup: `delete_worker` корректно убирает сеть, workspace, sidecar-контейнеры.
   - GC в scheduler: осиротевшие ресурсы (после OOM/restart) очищаются.
2. **Критерии успеха**:
   - Девелопер-агент завершает задачу без `/var/run/docker.sock`.
   - Все E2E-тесты оркестратора в CI проходят стабильно.
   - Нет `root`-owned файлов в workspace после работы sidecar'ов.
