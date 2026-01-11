# Migration Plan

Этот документ описывает стратегию рефакторинга и миграции проекта `codegen_orchestrator` на новую архитектуру.

## Philosophy: Hardcore TDD

Мы используем подход **Outside-In TDD** с фокусом на изоляцию сервисов. Мы не пишем код реализации, пока не написан и не упал соответствующий тест.

1.  **Contract First**: Сначала фиксируем DTO и поведение в контрактах.
2.  **Red (Service Level)**: Пишем интеграционный тест сервиса ("черный ящик"), который падает.
3.  **Red (Unit Level)**: Пишем юнит-тест для внутренней логики, который падает.
4.  **Green**: Реализуем ровно столько кода, чтобы тесты прошли.
5.  **Refactor**: Улучшаем код, не ломая тесты.

---

## The "Cube" (Deployment Unit)

"Кубик" — это автономная единица развертывания (сервис или пакет). Каждый сервис должен быть самодостаточным, включая свои тесты.

### Structure

```text
services/<service-name>/
├── src/                  # Исходный код
│   ├── main.py
│   └── ...
├── tests/                # Тесты кубика
│   ├── unit/             # Изолированные тесты классов
│   │   └── test_logic.py
│   ├── integration/      # Тесты API сервиса (Service Specs)
│   │   ├── conftest.py   # Фикстуры (Redis, MockRunner)
│   │   └── test_flow.py  # Сценарии из Service.md
│   └── conftest.py       # Общие фикстуры
├── Dockerfile            # Инструкция сборки
└── pyproject.toml        # Явные зависимости кубика
```

### Definition of Done (DoD) for a Cube

1.  [ ] **Specs Check**: Контракты в `CONTRACTS.md` и `services/<name>.md` согласованы.
2.  [ ] **Integration Tests**: Написаны и проходят тесты в `services/<name>/tests/integration`.
    *   Mock внешних зависимостей (Redis Pub/Sub, External APIs).
    *   Проверка всех сценариев (Happy Path, Error Cases).
3.  [ ] **Unit Tests**: Покрытие сложной бизнес-логики юнит-тестами.
4.  [ ] **Linter**: Проходит `ruff` / `mypy`.

---

## Migration Phases

Миграция выполняется строго последовательно, чтобы накапливать работающий функционал.

### Phase 0: Foundation (Shared Kernel)
*Без этого этапа невозможно написание тестов.*

1.  **Shared Logic**:
    *   `shared/contracts`: Перенос и актуализация всех Pydantic моделей.
    *   `shared/redis`: Обертка над Redis (Streams/Queue) с поддержкой тестирования.
    *   `shared/logging`: Настройка Structlog.
    *   **Thin API Refactor**:
        *   Удаление логики Redis Publisher из API (POST `/tasks`).
        *   Удаление обращений к GitHub/GitLab (оставить только CRUD БД).
        *   API становится чистым "Data Access Layer".

### Phase 1: Base Components & Infrastructure
*Строительные блоки для воркеров и деплоя.*

2.  **Orchestrator CLI** (`packages/orchestrator-cli`):
    *   *Action*: Выделение кода из `shared/cli`.
    *   *Action*: Добавление `pyproject.toml`.
    *   *Feature*: Реализация прямой публикации в Redis (`XADD` в `engineering:queue`).
    *   *Test*: Unit tests для команд + Mock Redis.

3.  **Worker Wrapper** (`packages/worker-wrapper`):
    *   *New Package*: "Нервная система" воркера.
    *   *Feature*: Реализация Loop `XREAD` -> Subprocess (Agent) -> `XADD`.
    *   *Feature*: Публикация событий `worker:lifecycle` (Started, Completed, Failed, Stopped).
    *   *Test*: Integration tests с реальным Redis и Mock Agent.

4.  **Infra Service** (Official Rename from `infrastructure-worker`):
    *   *Refactor*: Переименование директорий и сервиса.
    *   *Feature*: Подписка на `provisioner:queue` и `ansible:deploy:queue`.
    *   *Test*: Mock Ansible Runner.

5.  **Worker Manager**:
    *   *Dep*: Зависит от образов с CLI и Wrapper.
    *   *Feature*: Сборка `worker-base` образа (включая CLI и Wrapper).
    *   *Feature*: Реализация `Activity Tracking` через `worker:lifecycle`.

### Phase 2: Core Logic
*Бизнес-логика оркестрации.*

4.  **Scaffolder**: Генерация кода.
    *   Тестируется с MockCopier/Git.
7.  **LangGraph Service**: Мозговой центр.
    *   *Architecture*: Реализация паттерна "Single-Listener" (только `worker:developer:output`).
    *   *Feature*: `ResponseListener` слушающий `worker:developer:output` (включая failure-события от worker-manager).
    *   *Feature*: Механизм восстановления состояния (Interrupt & Resume) через Postgres.
    *   *Feature*: Retry-логика при получении `status="failed"` от воркера.

### Phase 3: Access & UI
*Точки входа.*

83.  **Telegram Bot**: Интерфейс пользователя. (Moved from later phase)
7.  **Telegram Bot**: Интерфейс пользователя.
    *   Тестируется через симуляцию сообщений Telegram.

### Phase 4: Big Assembly (E2E)
*Сборка системы воедино.*

8.  **System E2E Tracing**:
    *   Тесты в корневой папке `tests/e2e/`.
    *   `docker-compose up` всей системы.
    *   Прогон полного цикла: "User -> Bot -> PO -> Spec -> Dev -> Deploy -> URL".