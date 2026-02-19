# Plan: Dev Environment Architecture Migration

> **Дата**: 2026-02-19
> **Контекст**: Миграция архитектуры воркеров с Docker-in-Docker на нативную разработку (sidecar-инфраструктура по запросу) для надежности пайплайнов и ускорения работы LLM-агентов. Вариант инфры: flat dev environment с честным `docker compose up` базы на каждый проект (Per-Worker) и нативным запуском остального кода через `uv`.

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
   - Обновить логику `worker_manager.delete_worker`:
     - Останавливать инфраструктуру проекта через `docker compose down -v` (с удалением volumes).
     - Удалять сеть `dev_proj_<worker_id>`.
     - Удалять директорию `/tmp/codegen/workspaces/<worker_id>/`.
   - В сервисе `scheduler` добавить фоновую джобу для периодической очистки "осиротевших" сетей `dev_proj_*`, volumes `worker_*` и воркспейсов на диске (на случай OOM или хард-рестарта оркестратора).

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
     - **Запрет проброса портов**: Директива `ports` блокируется для предотвращения конфликтов портов на хосту между воркерами (доступ к сервисам только по именам внутри `dev_proj_<worker_id>`).
   - **Трансляция путей**: Worker-manager запускает `docker compose` с:
     - `--project-directory` = `/tmp/codegen/workspaces/<worker_id>/workspace/<cwd>` (абсолютный путь на хосте).
     - `--project-name` = `worker_<worker_id>` (изоляция имён контейнеров между воркерами).
   - **Подключение к сети**: Все поднимаемые сервисы подключаются к `dev_proj_<worker_id>` (через `--network` или `COMPOSE_PROJECT_NETWORK` env).
   - **Порты**: Sidecar-сервисы **не публикуют порты** на хост. Доступ только по имени сервиса через общую сеть `dev_proj_<worker_id>`.
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
   - Нативно (внутри контейнера воркера через `uv run` или fallback `make`) выполняются:
     - Линтеры (`ruff`).
     - Юнит-тесты (`pytest tests/unit`).
     - Кодогенерация (`framework generate`, `sync_services`).

---

## Фаза 3: Адаптация Шаблонов (`service_template`)

Шаблоны должны быть готовы к нативному выполнению большей части операций. Интеграционные тесты остаются в Docker Compose.

1. **Рефакторинг `Makefile` в `service_template`**:
   - Ввести переменную `EXEC_MODE ?= docker` (по умолчанию — Docker для обратной совместимости, воркеры выставляют `EXEC_MODE=native`).
   - Адаптировать цели:
     - `make format` → `uv run ruff format .` (вместо `docker compose run tooling ruff format .`).
     - `make lint` → `uv run ruff check .`.
     - `make test-unit` → `uv run pytest tests/unit`.
     - `make generate-from-spec` → `uv run python -m framework.sync_services create && uv run python -m framework generate`.
   - Сохранить запуск интеграционных тестов через Docker Compose (`make test-integration` всегда через compose).

2. **Совместимость кодогенерации и безопасности**:
   - Убедиться, что `uv run python -m framework.sync_services create` запускается корректно в native-режиме без ошибок монтирования.
   - Добавить `healthcheck` ко всем инфраструктурным сервисам в compose-файлах шаблона (для корректной работы `--wait`).
   - Во всех compose-файлах шаблона `docker-compose.yml` явно прописать директиву `user: "${HOST_UID:-1000}:${HOST_GID:-1000}"` для баз данных, чтобы генерируемые ими файлы (dbs, кэши) не становились `root`-owned.

---

## Фаза 4: Удаление Docker-in-Docker и обновление промптов

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
   - Указать, что линтеры, генерация и юнит-тесты должны запускаться нативно через `uv run` / `make`.

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
