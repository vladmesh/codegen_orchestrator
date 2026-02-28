# Агенты и Ноды

Каждый агент — это узел LangGraph с собственным набором инструментов и специализацией.

---

## 🧭 Product Owner (LangGraph ReactAgent)

**Роль**: Центральный координатор. Управляет жизненным циклом проекта через API tools, единственная точка коммуникации с пользователем.

**Реализация**: LangGraph `create_react_agent` в `services/langgraph/src/po/`. Runs as an async consumer inside the langgraph container — no separate Docker container needed. Conversation state persisted via PostgreSQL checkpointer (`AsyncPostgresSaver`, schema `langgraph`); falls back to in-memory `MemorySaver` without `CHECKPOINT_DATABASE_URL`. Long conversations are compressed via `langmem.SummarizationNode` (`pre_model_hook`) — old messages are summarized into a running summary stored in `state["context"]` instead of being silently dropped.

**Инструменты** (`src/po/tools.py`):
- `create_project`, `list_projects`, `get_project`: управление проектами через API
- `set_project_secret`: сохранение секретов
- `trigger_engineering`, `trigger_deploy`: запуск subgraphs через API + Redis
- `get_task_status`: статус задач
- `set_reminder`: отложенные проверки через Redis ZSET
- `notify_user`: proactive message to user via `po:proactive` stream (Phase 2.3)

**Communication**: Redis streams — `po:input` (inbound, user messages + system events), `po:response:{request_id}` (outbound, sync replies), `po:proactive` (outbound, async notifications). All PO streams use Pydantic contracts from `shared.contracts.queues.po` (`POInputMessage`, `POResponse`, `POProactiveMessage`) with flat-field serialization (`to_flat_fields()` / `from_flat_fields()`). PO Consumer has PEL recovery via `XAUTOCLAIM` on startup. Workers write system events to `po:input` via `callback_stream`. PO uses `notify_user` tool to send proactive messages when handling system events.

**Выход**: Действия через tools, сообщения пользователю через Telegram

---


## 👨‍💻 Developer (Engineering Subgraph)

**Роль**: Написание бизнес-логики в уже scaffolded проекте.

**Когда вызывается**:
- Первый этап Engineering Subgraph
- При rework от Tester (до 3 итераций)

**Реализация**:
1. Engineering worker создает GitHub-репозиторий и устанавливает registry secrets (`_create_repo_and_set_secrets()`)
2. Developer node строит `ScaffoldConfig` (modules, project_name, task description) и передаёт worker spawner
3. Спавнит контейнер через `worker-manager` (Claude Code / Factory.ai) с `scaffold_config` в команде
4. Worker-manager выполняет scaffold phase внутри контейнера (`docker exec`: copier + make setup + git push), затем устанавливает `project.status = "scaffolded"`
5. Worker-manager инжектит инструкции из `services/langgraph/src/prompts/developer_worker/INSTRUCTIONS.md` и `TASK.md` с project-specific задачей
6. Агент клонирует scaffolded repo и пишет бизнес-логику

**Валидация**: Проверяет наличие commit SHA в результате.

**Выход**: Код в репозитории → Tester

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
├── nodes.py             # SecretResolver, ReadinessCheck, Deployer
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

**Proactive notifications** (webhook-triggered deploys):
Когда deploy запущен через webhook (нет `callback_stream`), deploy-worker отправляет результат напрямую в `po:proactive` → telegram-bot → пользователь. Сообщения: успех (deployed URL), missing secrets, ошибка.

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
     ├──────────────▶ trigger_engineering
     │                     │
     │                     ▼
     │               Engineering Subgraph
     │               eng-worker: create repo + secrets
     │                     │
     │                     ▼
     │               Developer node → worker-manager
     │               scaffold phase (copier + make setup)
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

**Важно**: PO ReactAgent координирует весь flow через LangChain tools. Engineering worker создаёт репозиторий и устанавливает registry secrets inline. Worker-manager выполняет scaffold phase (copier + make setup + git push) внутри worker-контейнера через docker exec перед запуском агента. Webhook-triggered deploys обходят PO — API публикует напрямую в deploy:queue, результат уходит через po:proactive.
