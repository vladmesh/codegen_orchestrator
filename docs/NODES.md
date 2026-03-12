# Агенты и Ноды

Каждый агент — это узел LangGraph с собственным набором инструментов и специализацией.

---

## 🧭 Product Owner (LangGraph ReactAgent)

**Роль**: Центральный координатор. Управляет жизненным циклом проекта через API tools, единственная точка коммуникации с пользователем.

**Реализация**: LangGraph `create_react_agent` в `services/langgraph/src/agents/po/`. Runs as an async consumer inside the langgraph container — no separate Docker container needed. Conversation state persisted via PostgreSQL checkpointer (`AsyncPostgresSaver`, schema `langgraph`); falls back to in-memory `MemorySaver` without `CHECKPOINT_DATABASE_URL`. Long conversations are compressed via `langmem.SummarizationNode` (`pre_model_hook`) — old messages are summarized into a running summary stored in `state["context"]` instead of being silently dropped.

**Инструменты** (`src/agents/po/tools.py`):
- `create_project`, `list_projects`, `get_project`: управление проектами через API
- `set_project_secret`: сохранение секретов
- `validate_telegram_token`: validates Telegram bot token via `getMe` API, extracts bot username, stores both token and username as project secrets. Invalid tokens fail fast at PO stage.
- `create_story`: создание user story + автоматический запуск engineering work
- `reopen_story`: переоткрытие завершённой story с user_report (контекст проблемы)
- `list_stories`, `get_story`: просмотр stories, привязанных tasks и их runs (с id, status, type, error, timing)
- `get_run_status`: детальный статус конкретного engineering/deploy run
- `set_reminder`: отложенные проверки через Redis ZSET
- `notify_user`: proactive message to user via `po:proactive` stream
- `web_search`: поиск документации внешних API через DuckDuckGo

**System events**: PO consumer принимает три story-level события: `story_completed` (deploy success), `story_failed` (permanent failure after retries), `story_blocked` (developer hit a blocker, WAITING_HUMAN_REVIEW). Все остальные system events дропаются — PO проверяет прогресс через reminders.

**Communication**: Redis streams — `po:input` (inbound, user messages + system events), `po:response:{request_id}` (outbound, sync replies), `po:proactive` (outbound, async notifications). All PO streams use Pydantic contracts from `shared.contracts.queues.po` (`POInputMessage`, `POResponse`, `POProactiveMessage`) with flat-field serialization (`to_flat_fields()` / `from_flat_fields()`). PO Consumer has PEL recovery via `XAUTOCLAIM` on startup. Workers write system events to `po:input` via `callback_stream`. PO uses `notify_user` tool to send proactive messages when handling system events.

**Выход**: Действия через tools, сообщения пользователю через Telegram

---


## 👨‍💻 Developer (Engineering Subgraph)

**Роль**: Написание бизнес-логики в уже scaffolded проекте.

**Когда вызывается**:
- Первый этап Engineering Subgraph
- При rework от Tester (до 3 итераций)

**Реализация**:
1. Scaffolder service (отдельный микросервис) выполняет scaffold phase: copier + make setup + git push, сохраняет tree + specs_summary в DB, ставит `project.status = active`
2. Architect Consumer (langgraph) ждёт завершения scaffold (poll project.status != draft, до 5 мин), затем декомпозирует story в tasks (видит tree, specs summary: модели, домены, события)
3. Task Dispatcher находит разблокированные tasks, создаёт Runs, публикует в `engineering:queue`
4. Engineering worker создает GitHub-репозиторий и устанавливает registry secrets
5. Спавнит контейнер через `worker-manager` (Claude Code / Factory.ai)
6. Worker-manager инжектит инструкции из `services/langgraph/src/prompts/developer_worker/INSTRUCTIONS.md` и `TASK.md` с project-specific задачей
7. Агент клонирует scaffolded repo и пишет бизнес-логику

**Валидация**: Проверяет наличие commit SHA в результате.

**Обработка блокеров**: Если developer agent не может выполнить задачу (missing credentials, 404 URLs, contradictory requirements), он запускает `orch report-blocker --reason "..."`. Worker-wrapper парсит маркер `## BLOCKED` и возвращает `block_reason` в результате. Developer node возвращает `engineering_status="developer_blocked"`. Engineering consumer вызывает `_handle_worker_blocked()`:
- Task → `waiting_human_review` с `failure_metadata = {failure_reason: "developer_blocked", block_reason: ...}`
- Story → `waiting_human_review`
- Уведомление admin через `notify_admins()` (level=warning)
- Уведомление пользователя через PO (`story_blocked` event)
- Worker container **не удаляется** (admin может инспектировать)

Для возобновления: `POST /tasks/{id}/resume` (admin даёт guidance, task WHR → IN_DEV).

**Выход**: Код в репозитории → Tester | Или `developer_blocked` → WHR flow

---

## 🧪 Tester (Engineering Subgraph)

**Роль**: Запуск тестов, проверка качества кода.

**Когда вызывается**:
- После Developer
- Финальный этап Engineering Subgraph

**Действия**:
- Запуск `make test`, `make lint`
- Проверка health endpoints (если задеплоено)

**Выход**:
- `test_results` с passed/failed/skipped
- При неудаче → возврат к Developer (max 3 итерации)
- При успехе → `engineering_status="done"` → DevOps

---

## 🔧 DevOps (Subgraph)

**Роль**: Деплой с интеллектуальным анализом секретов.

**Когда вызывается**:
- После Engineering Subgraph
- При `trigger_deploy` от PO
- При GitHub webhook (`workflow_run: ci.yml success on main`) → API → deploy:queue

**Структура пакета** (`src/subgraphs/devops/`):
```
devops/
├── __init__.py          # Экспорты
├── state.py             # DevOpsState TypedDict
├── env_analyzer.py      # EnvAnalyzer + helper функции
├── env_groups.py        # EnvGroup ABC, PostgresGroup, RedisGroup, resolve_with_groups
├── nodes.py             # SecretResolver, ReadinessCheck, Deployer, SmokeTester
└── graph.py             # Routing + create_devops_subgraph
```

**Ноды внутри subgraph**:

1. **EnvAnalyzer (LLM)**: Анализирует .env.example, классифицирует переменные
   - `infra`: генерируются автоматически (REDIS_URL, DATABASE_URL)
   - `computed`: вычисляются из контекста (APP_NAME, APP_ENV)
   - `user`: запрашиваются у пользователя (TELEGRAM_BOT_TOKEN)

2. **SecretResolver (Functional)**:
   - Дешифрует существующие секреты из БД (`decrypt_dict`)
   - Двухфазная резолюция infra-переменных:
     * Фаза 1: cached secrets из `config_secrets` (приоритет)
     * Фаза 2: uncached → `resolve_with_groups()` (когерентные пароли для связанных переменных, например DATABASE_URL + POSTGRES_PASSWORD) → fallback `_generate_infra_secret()` для остальных
   - Подставляет computed значения, проверяет наличие user секретов
   - Шифрует и сохраняет новые секреты обратно в БД (`encrypt_dict`)

3. **ReadinessCheck (Functional)**:
   - Проверяет готовность к деплою
   - Если есть missing_user_secrets → возврат к PO
   - Если всё готово → Deployer

4. **Deployer (Functional)**:
   - Собирает DOTENV из resolved_secrets (`build_dotenv` → `encode_dotenv` → base64)
   - Записывает 9 GitHub Secrets: DOTENV, DEPLOY_HOST, DEPLOY_USER, DEPLOY_SSH_KEY, DEPLOY_PORT, PROJECT_NAME, REGISTRY_URL, REGISTRY_USER, REGISTRY_PASSWORD
   - Тригерит `deploy.yml` через `trigger_workflow_dispatch`
   - Ждёт завершения через `wait_for_workflow_completion` (poll, timeout 600s)
   - Post-deployment операции:
     * Создает service deployment record в БД (с `deployed_sha`)
     * Устанавливает статус проекта = active

5. **SmokeTester (Functional)**:
   - Делает HTTP `/health` check для бекендов и Telethon `/start` check для tg_bot модулей.
   - Реализует retry logic (3 попытки, 5s delay) и graceful skip.
   - On failure: SSHes into deploy server, captures `docker compose logs --tail=50`, appends to check `detail` field. Logs flow through deploy→engineering feedback loop so fix tasks get actual tracebacks.
   - Записывает `smoke_result` в `DevOpsState` для проброса статуса в deploy-worker.

**Архитектура**:
```
Deployer → build_dotenv → set_repository_secrets (GitHub API)
                        → trigger_workflow_dispatch (deploy.yml)
                        → wait_for_workflow_completion (poll)
                                       ↓
                              GitHub Actions Runner
                                       ↓
                              Docker build + deploy to VPS
```

**Выход**:
- `deployed_url` при успехе
- `missing_user_secrets` если нужны секреты от пользователя

**Proactive notifications**:
Filtered to reduce spam — only two events reach user via `po:proactive`: (1) deploy success (deployed URL), (2) permanent story failure (user-friendly message). All intermediate failures (smoke, precheck, workflow) are routed through the deploy→engineering feedback loop for automated fixing.

**Deploy→Engineering Feedback Loop**:
When deploy succeeds but smoke test fails, or workflow fails entirely, deploy worker re-dispatches a fix task to `engineering:queue`. Capped at 2 retry attempts via `deploy_fix_attempt` counter. After limit, story fails permanently and user is notified.

---

## 🚧 Infra Service

**Роль**: Изолированный сервис для выполнения Ansible операций (provisioning).

**Реализация**: Отдельный сервис `infra-service` для изоляции тяжёлых зависимостей (Ansible, SSH).

**Типы jobs**:
1. **Provisioning** (`provisioner:queue`):
   - Password reset через Time4VPS API
   - OS reinstall при необходимости
   - Ansible playbooks для настройки сервера
   - Редеплой сервисов после восстановления

**Архитектура**:
```
infra-service
  ├── Listen: provisioner:queue (RedisStreamClient.consume, auto_ack=False, claim_pending=True)
  ├── Handlers:
  │   └── process_provisioner_job() → ansible_runner.py
  └── Publish: provisioner:results
```

**Выход**: Результаты в Redis Stream `provisioner:results`

---

## 🔄 Взаимодействие

```
Пользователь (Telegram)
     │
     ▼
Telegram Bot → Redis (po:input)
     │
     ▼
PO ReactAgent (in langgraph container)
     │ tool calls (httpx/Redis)
     ├──────────────▶ po:response:{request_id} ──▶ Пользователь
     │
     ├──────────────▶ scaffold:queue → Scaffolder Service
     │               (copier + make setup + git push, saves tree + specs_summary)
     │                     │
     │                     ▼
     │               architect:queue → Architect Consumer
     │               (waits for scaffold, then LLM: story → tasks with specs context)
     │                     │
     │                     ▼
     │               Task Dispatcher → engineering:queue
     │                     │
     │                     ▼
     │               Engineering Subgraph
     │               eng-worker: create repo + secrets
     │                     │
     │                     ▼
     │               Developer node → worker-manager
     │               → agent writes code → Tester
     │                                     │
     ├──────────────▶ trigger_deploy ◄─────┘
     │                     │
     │                     ▼
     │               DevOps Subgraph
     │               EnvAnalyzer → SecretResolver → ReadinessCheck → Deployer
     │                                                      │
     └──────────────▶ (завершение) ◄─────────────────────────┘


GitHub (webhook: ci.yml success on main)
     │
     ▼
API: POST /webhooks/github
     │ verify HMAC → lookup project → create Task
     ▼
Redis (deploy:queue) → deploy-worker → DevOps Subgraph
     │
     ▼
Redis (po:proactive) → Telegram Bot → Пользователь
```

**Важно**: PO ReactAgent координирует весь flow через LangChain tools. Scaffolder (отдельный сервис) подготавливает репозиторий (copier + make setup + git push) до запуска architect. Worker-manager монтирует pre-scaffolded workspace volume из `/data/workspaces/{repo_id}/` в контейнер воркера. Webhook-triggered deploys обходят PO — API публикует напрямую в deploy:queue, результат уходит через po:proactive.
