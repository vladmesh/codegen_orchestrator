# Admin Panel v2: Actions + Config Externalization

> [!WARNING]
> Этот файл автогенерируется командой `make sync`. Не редактируйте вручную — изменения будут перезаписаны.

# Brainstorm: Admin Panel v2 — Actions + Config Externalization

> **Дата**: 2026-03-19
> **Контекст**: Расширить админку экшенами (deploy, scaffold, e2e, worker spawn) и вынести захардкоженные конфиги/промпты в БД для hot-reload
> **Связано с**: bs-admin-panel (v1, done), backlog #1006 (decouple deploy from story)
> **Status**: draft

---

## Current State

### Админка сейчас (v1)
React SPA, 9 страниц: Dashboard, Users, Projects, Tasks, Workers, Queues, Servers, Logs, Tracing. Действия ограничены:
- **Tasks**: retry (→backlog), resume (с guidance)
- **Queues**: XACK/XDEL сообщений
- **Servers**: force-rebuild, provision, update fields, allocate port

Нет: создание приложения, deploy, undeploy, e2e, spawn worker, создание story, управление секретами.

### Захардкоженное

**Промпты** (4 штуки, все в Python-файлах):
- PO system prompt — `services/langgraph/src/prompts/po/__init__.py` (176 строк)
- Architect prompt — `services/langgraph/src/prompts/architect/__init__.py` (78 строк)
- Developer worker — `services/langgraph/src/prompts/developer_worker/INSTRUCTIONS.md` (150+ строк)
- Deploy failure classifier — `services/langgraph/src/consumers/deploy_failure_handler.py:29-46`

**Таймауты и пороги** (в Python-константах):
- `DISPATCH_INTERVAL_SECONDS = 30` (task_dispatcher.py)
- `SYNC_INTERVAL = 300` (github_sync.py)
- `SERVER_SYNC_INTERVAL = 60`, `DETAILS_SYNC_INTERVAL = 300` (server_sync.py)
- `STORY_STUCK_THRESHOLD_MINUTES = 5`, `TASK_STUCK_THRESHOLD_MINUTES = 30` (supervisor.py)
- `MAX_DEPLOY_FIX_ATTEMPTS = 2`, `MAX_DEPLOY_RETRIES = 3` (deploy_failure_handler.py)
- `CONSECUTIVE_FAILURE_THRESHOLD = 3`, `SSL_EXPIRY_WARNING_DAYS = 7` (app_health_prober.py)
- `RAM_THRESHOLD_PCT = 90`, `DISK_THRESHOLD_PCT = 90` (health_checker.py)
- `RETENTION_HOURS = 168` (health_checker.py)

**LLM-конфиги** (частично в agent_configs, но не всё):
- `agent_configs` таблица уже есть (model, temperature, system_prompt, provider)
- НО: deploy_failure_handler использует свой хардкод (`anthropic/claude-haiku-4-5`, temp=0.0)
- PO summarization defaults хардкожены в graph.py (trigger_tokens=60000, etc.)
- Task max_iterations по умолчанию 3 — хардкод в модели, схеме и orchestrator schema

---

## Problem / Opportunity

### 1. Админка — read-only dashboard
Сейчас 90% страниц — просмотр. Оператор не может выполнить типичные задачи без API/CLI:
- Создать приложение из существующего репо и задеплоить
- Обновить секреты проекта
- Запустить e2e на приложение
- Остановить/переразвернуть приложение
- Заспавнить воркера с таской
- Создать стори и передать архитектору

### 2. Конфиги требуют редеплоя
Чтобы поменять порог "task stuck > 30 min" или промпт архитектора — нужен коммит, билд, деплой. Для быстрой итерации по промптам это неприемлемо.

### 3. Изоляция бизнес-логики
Если "создать приложение и задеплоить" трудно вызвать из админки — значит эта логика зашита внутри consumer-цепочки и плохо изолирована. Это хороший момент для рефакторинга: каждое действие должно быть вызываемой функцией/API-эндпоинтом.

---

## Part 1: Config Externalization

### Модель: `SystemConfig`

Новая таблица/модель для key-value конфигурации:

```
system_configs
  key: str (PK)          — e.g. "supervisor.task_stuck_threshold_minutes"
  value: JSON             — числа, строки, объекты
  description: str        — human-readable описание
  category: str           — группировка (scheduler, llm, health, deploy)
  updated_at: datetime
  updated_by: str
```

### Что выносить

**Категория `scheduler`**:
- `dispatch_interval_seconds` = 30
- `github_sync_interval` = 300
- `server_sync_interval` = 60
- `server_details_sync_interval` = 300

**Категория `supervisor`**:
- `story_stuck_threshold_minutes` = 5
- `task_stuck_threshold_minutes` = 30
- `story_max_architect_retries` = 3
- `story_retry_ttl` = 3600

**Категория `deploy`**:
- `max_deploy_fix_attempts` = 2
- `max_deploy_retries` = 3
- `deploy_retry_ttl` = 86400

**Категория `health`**:
- `ram_threshold_pct` = 90
- `disk_threshold_pct` = 90
- `consecutive_failure_threshold` = 3
- `ssl_expiry_warning_days` = 7
- `metrics_retention_hours` = 168

**Категория `llm`**:
- `summarization_trigger_tokens` = 60000
- `summarization_max_tokens` = 50000
- `summarization_max_summary_tokens` = 2000
- `task_default_max_iterations` = 3

### Промпты

Промпты **уже** ложатся в `agent_configs.system_prompt`. Нужно:
1. Перенести PO, Architect, Developer промпты в `agent_configs` (если ещё не там)
2. Deploy failure classifier prompt → отдельный `agent_config` запись
3. Код читает промпт из БД, fallback на хардкод (для первого запуска / миграции)

### Как читать: config helper

```python
# shared/config_store.py
class ConfigStore:
    """Reads system_configs from API with in-memory cache + TTL."""

    def get(self, key: str, default: Any = None) -> Any:
        """Get config value, cached for CACHE_TTL seconds."""
        ...

    def get_category(self, category: str) -> dict[str, Any]:
        """Get all configs in a category."""
        ...
```

TTL-кеш (30-60 секунд) — конфиги не меняются чаще. Не нужен Redis pub/sub — слишком сложно для выигрыша.

### Fail-fast при старте

**Стек НЕ поднимается** если ключевые конфиги не заполнены в БД. Каждый сервис при старте:
1. Запрашивает свою категорию конфигов через API
2. Валидирует что все required keys присутствуют
3. Если нет — `RuntimeError("Missing system configs: ...")`, контейнер падает

Required configs определяются per-service:
- scheduler: `scheduler.*`, `supervisor.*`
- deploy-worker: `deploy.*`
- health_checker: `health.*`
- langgraph: `llm.*` + agent_configs для PO/Architect

Seed script (`make seed`) заполняет дефолтные значения — один раз при первом запуске.

### Prompt versioning

`agent_configs` уже имеет `version` (auto-increment при PATCH). Этого достаточно для аудита. Полный rollback UI не нужен — если промпт сломали, ручной откат через PATCH с предыдущим текстом.

### Админка: страница Settings

Таблица с группировкой по category. Inline edit. Кнопка Save. Показывать description и текущее значение. Для промптов — textarea с подсветкой.

---

## Part 2: Admin Actions

### Принцип: API остаётся тонким — validate → write DB → publish Redis

API **не содержит бизнес-логику**. API делает три вещи:
1. Валидирует входные данные
2. Пишет/читает БД (создаёт записи, меняет статусы)
3. Публикует сообщение в Redis queue

Бизнес-логика живёт в consumer соответствующего сервиса. Admin UI → API → Redis → Consumer.

**Рефакторинг**: Там где consumer сейчас ожидает конкретный контекст (story, PO flow) — расширить контракт сообщения, чтобы consumer мог обработать standalone trigger. Не переносить логику, а сделать consumers более гибкими.

### Action 1: Create Application from Existing Repo

**Что нужно**: Привязать GitHub repo к проекту, задеплоить на сервер.

**API endpoint**: `POST /api/applications/from-repo`
- Валидация: repo_url валиден, server_handle существует, проект существует
- DB: create Repository → create Application → allocate port
- Redis: publish `deploy:queue` с `DeployMessage(app_id, server_handle, ...)`

**Consumer refactoring**: Deploy worker сейчас привязан к story lifecycle (#1006). Нужно чтобы `DeployMessage` контракт поддерживал `story_id: Optional` — если None, deploy без story transitions.

**UI**: Форма на странице Projects: repo URL, выбор сервера.

### Action 2: Update Secrets

**Что уже есть**: `POST /api/projects/{id}/config/secrets` — atomic merge. API пишет в БД — это его прямая ответственность, consumer не нужен.

**Нехватает**: `DELETE /api/projects/{id}/config/secrets/{key}`.

**UI**: Project Details → masked key-value editor.

### Action 3: Run E2E on Existing App

**API endpoint**: `POST /api/applications/{id}/run-e2e`
- DB: create Run (type=e2e, status=queued)
- Redis: publish `qa:queue` с `QAMessage(app_id, run_id, ...)`

**Consumer refactoring**: QA consumer сейчас ожидает `story_id`. Расширить контракт: `story_id: Optional`. Если None — standalone E2E, результат пишется только в Run, без story transitions.

**UI**: Кнопка "Run E2E" на Application Details. Poll Run status.

### Action 4: Stop Application / Remove from Server

**API endpoint**: `POST /api/applications/{id}/stop` и `POST /api/applications/{id}/undeploy`
- DB: update Application status → `stopping` / `undeploying`
- Redis: publish `deploy:queue` с `DeployMessage(action="stop"|"undeploy", app_id, ...)`

**Consumer refactoring**: Deploy worker получает новый `action` field в контракте. Сейчас только "deploy" (implicit). Добавить:
- `action="stop"` → SSH → `docker compose stop`, update status → `stopped`
- `action="undeploy"` → SSH → `docker compose down`, release ports (через API call), update status → `not_deployed`

**UI**: Кнопки "Stop" / "Undeploy" на Application Details. Confirmation dialog.

### Action 5: Redeploy Application

**API endpoint**: `POST /api/applications/{id}/redeploy`
- DB: create new Deployment record (status=pending)
- Redis: publish `deploy:queue` с `DeployMessage(action="deploy", app_id, ...)`

Самый простой action — reuse существующего deploy flow.

**UI**: Кнопка "Redeploy" на Application Details.

### Action 6: Spawn Worker with Task

**API endpoint**: `POST /api/tasks/{id}/spawn-worker`
- Валидация: task существует, status подходит, repository привязан
- DB: update task status → `in_dev`, create Run (type=engineering, status=queued)
- Redis: publish `engineering:queue` с `EngineeringMessage(task_id, run_id, ...)`

**Consumer refactoring**: Engineering consumer сейчас тригерится через task_dispatcher (poll loop). Убедиться что consumer обрабатывает сообщение из очереди идентично — он уже должен, т.к. dispatcher публикует то же сообщение. Если dispatcher добавляет контекст (workspace path, etc.) — этот контекст должен либо быть в сообщении, либо consumer должен уметь его получить сам.

**UI**: Кнопка "Spawn Worker" на Task Details. На Projects — "Create Task + Spawn" (два API call с фронта).

### Action 7: Create Story → Architect

**API endpoint**: `POST /api/stories/{id}/send-to-architect`
- Валидация: story существует, status = created/reopened
- DB: update story status → `in_progress`
- Redis: publish `architect:queue` с `ArchitectMessage(story_id, ...)`

**Consumer refactoring**: Architect consumer в scheduler уже читает из `architect:queue`. Убедиться что формат сообщения одинаков с тем что публикует PO agent.

**UI**: На Stories — кнопка "Send to Architect". На Projects — "Create Story" форма + кнопка.

---

## Part 3: Consumer Refactoring for Standalone Triggers

### Текущие проблемы

| Action | Consumer | Проблема |
|--------|----------|----------|
| Deploy | deploy-worker | `DeployMessage` привязан к story — story transitions внутри consumer (#1006) |
| QA/E2E | qa consumer (в langgraph) | Ожидает story_id в payload |
| Spawn worker | engineering consumer | Тригерится только через task_dispatcher, но формат сообщения скорее всего совместим |
| Story → Architect | architect_consumer (scheduler) | Тригерится через PO tool, формат нужно проверить |
| Stop/Undeploy | Не существует | Новый action type в deploy-worker |

### Паттерн рефакторинга: Optional context в контрактах

Не создавать service layer. Вместо этого — расширить контракты сообщений:

```python
# shared/contracts/queues/deploy.py
class DeployMessage(BaseModel):
    action: Literal["deploy", "stop", "undeploy"] = "deploy"
    application_id: int
    server_handle: str
    story_id: str | None = None          # ← was required, now optional
    project_id: str
    # ...

# shared/contracts/queues/qa.py
class QAMessage(BaseModel):
    application_id: int
    run_id: str
    story_id: str | None = None          # ← optional for standalone
    # ...
```

Consumer проверяет: `if message.story_id: do_story_transitions() else: skip`.

Это минимальный рефакторинг — не ломает существующий flow, просто делает его гибче.

---

## Implementation Order

### Phase 1: Config Externalization (фундамент)
1. Модель `SystemConfig` + миграция
2. CRUD API endpoints (`/api/system-configs/`)
3. Seed script: заполняет все ~25 конфигов дефолтными значениями (`make seed`)
4. `ConfigStore` helper с TTL-кешем (shared/ — только чтение через HTTP, без бизнес-логики)
5. Fail-fast: каждый сервис при старте валидирует required configs, падает если нет
6. Переключить сервисы с хардкодов на ConfigStore (по одному сервису за раз)
7. Admin UI: Settings page

### Phase 2: Queue contract refactoring (prerequisite для кнопок)
8. #1006: Decouple deploy worker — `story_id: Optional` в DeployMessage, skip story transitions if None
9. DeployMessage: добавить `action: Literal["deploy", "stop", "undeploy"]`
10. QAMessage: `story_id: Optional` для standalone E2E
11. Убедиться что engineering consumer работает с прямым publish (без dispatcher context)
12. Убедиться что architect consumer формат совместим с PO agent publish

### Phase 3: Thin API endpoints (validate → DB → Redis)
13. `POST /api/stories/{id}/send-to-architect` — DB status + publish `architect:queue`
14. `POST /api/tasks/{id}/spawn-worker` — DB status + Run + publish `engineering:queue`
15. `POST /api/applications/{id}/stop` — DB status + publish `deploy:queue` (action=stop)
16. `POST /api/applications/{id}/undeploy` — DB status + publish `deploy:queue` (action=undeploy)
17. `POST /api/applications/{id}/redeploy` — Deployment record + publish `deploy:queue`
18. `POST /api/applications/{id}/run-e2e` — Run record + publish `qa:queue`
19. `POST /api/applications/from-repo` — Repository + Application + port + publish `deploy:queue`
20. `DELETE /api/projects/{id}/config/secrets/{key}`

### Phase 4: Admin UI
21. Settings page (config + prompt editor)
22. Project Details: Secrets editor, "Create Story", "Deploy from Repo"
23. Story Details: "Send to Architect" button
24. Task Details: "Spawn Worker" button
25. Application Details: "Stop", "Undeploy", "Redeploy", "Run E2E" buttons

---

## Risks

- **#1006 blocker**: Standalone deploy/stop/undeploy — всё зависит от decoupling story из deploy worker. Это первый рефакторинг в Phase 2.
- **Security**: Admin actions мощные. Audit trail — расширить существующий event model на все действия (actor = "admin:user_id").
- **Config availability**: Если API недоступен при старте сервиса — сервис не поднимется. Это правильно (fail-fast), но нужна правильная очередность в docker-compose (depends_on + healthcheck).
- **Migration path**: Первый деплой после добавления SystemConfig — нужен `make seed` перед перезапуском сервисов. Документировать в CHANGELOG.

---

## Action Items

- → new task: "SystemConfig model + CRUD API + seed script + fail-fast validation" (Phase 1)
- → new task: "ConfigStore helper with TTL cache — read configs from API" (Phase 1)
- → new task: "Switch services from hardcoded constants to ConfigStore" (Phase 1 — по сервисам)
- → new task: "Admin UI: Settings page (config + prompt editor)" (Phase 4)
- → backlog #1006: Decouple deploy worker from story lifecycle (Phase 2 prerequisite)
- → new task: "Queue contracts: Optional story_id + action field in DeployMessage/QAMessage" (Phase 2)
- → new task: "Thin API endpoints for admin actions (7 endpoints)" (Phase 3)
- → new task: "Admin UI: action buttons on all entity pages" (Phase 4)
- → new task: "Admin UI: secrets editor on Project Details" (Phase 4)

