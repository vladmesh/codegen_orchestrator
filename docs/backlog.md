# Backlog

> Актуально на: 2025-12-30

## Technical Debt (Активная работа)

### PO does not wait for async deploy completion

**Priority:** MEDIUM  
**Status:** TODO

**Проблема:** Когда PO вызывает `trigger_deploy`, он получает `job_id` и сразу возвращает его пользователю с сообщением "используй `get_deploy_status`". Это плохой UX:
1. Пользователь получает технический `job_id` и должен вручную спрашивать статус
2. PO завершает сессию (`awaiting=True`) вместо ожидания результата
3. Когда деплой завершается, PO не уведомляется

**Текущий flow:**
```
PO → trigger_deploy → job_id
PO → respond_to_user("Task ID: xxx, use get_deploy_status...")
PO → session ends (awaiting user input)
Deploy worker → completes → nobody notifies user
```

**Желаемый flow:**
```
PO → trigger_deploy → job_id
PO → respond_to_user("Deploy started, please wait...")
Deploy worker → completes → publishes event
LangGraph → receives event → wakes up PO
PO → respond_to_user("Deploy successful! URL: ...")
```

**Решение:** Event-driven wake-up — Deploy worker публикует `deploy:complete:{thread_id}` event, LangGraph слушает и инжектит сообщение в thread PO.

**Что уже есть:**
- `listen_worker_events` в `worker.py` подписывается на `worker:events:all`
- Нужно роутить deploy completion events обратно в PO's thread
- Возможно нужно новое поле в state: `pending_deploys: list[str]`

---

### Separate tooling into dedicated compose file

**Priority:** HIGH  
**Status:** TODO  
**Location:** `docker-compose.yml`, `Makefile`

**Проблема:** Tooling (`ruff format`, `ruff check`) находится в основном `docker-compose.yml` с профилем `dev`. При запуске `make format` или `make lint` команда `docker compose --profile dev down --remove-orphans` **убивает весь рабочий стек** (api, langgraph, db, redis...).

**Причина:** Docker Compose с профилями + `--remove-orphans` работает агрессивно:
1. `--profile dev` заставляет compose "видеть" только сервисы с этим профилем (tooling)
2. `--remove-orphans` удаляет контейнеры проекта, которых нет в текущем "view"
3. Все остальные сервисы (api, langgraph, etc.) считаются orphans и удаляются

**Воспроизведение:**
```bash
make up                                              # Поднять стек
docker compose --profile dev down --remove-orphans --dry-run  # Увидеть что ВСЁ будет убито
```

**Решение:** Вынести tooling в отдельный compose файл:

```
docker-compose.yml          # Основной стек (api, langgraph, db, redis...)
docker-compose.test.yml     # Тесты (уже есть, с -p для изоляции)
docker-compose.tools.yml    # Tooling (ruff, uv lock) — НОВЫЙ
```

**Изменения в Makefile:**
```makefile
DOCKER_COMPOSE_TOOLS := docker compose -f docker-compose.tools.yml

format:
    @$(DOCKER_COMPOSE_TOOLS) run --rm tooling ruff format ...
    # Никаких down, никаких --remove-orphans
    # --rm уже удалит контейнер после завершения
```

**Преимущества:**
1. Изоляция — невозможно случайно убить рабочий стек при линтинге
2. Ясность — основной compose = продакшн/дев стек, tools = инструменты разработки
3. Проще Makefile — не нужны хаки с профилями
4. Разные жизненные циклы — стек живёт долго, tooling запускается на секунды

---

### Fix datetime serialization in worker events forwarding

**Priority:** LOW  
**Status:** TODO  
**Location:** `services/langgraph/src/worker.py:395`

Worker events (started, progress, completed, failed) не пересылаются в stream `orchestrator:events` из-за ошибки сериализации datetime.

**Причина:**
```python
# Текущий код
await publish_event(f"worker.{event.event_type}", event.model_dump())
```

`WorkerEvent` содержит поле `timestamp: datetime`. При вызове `model_dump()` datetime остаётся объектом Python, а `json.dumps()` в `RedisStreamClient.publish()` (строка 79) не умеет его сериализовать.

**Исправление:**
```python
await publish_event(f"worker.{event.event_type}", event.model_dump(mode="json"))
```

---

## Future Improvements (Extracted from archived plans)

### Telegram Bot Pool (Resource Allocation)

**Priority:** MEDIUM  
**Status:** TODO  
**Source:** secrets-and-project-filtering-refactor.md (Iteration 3)

Автоматическое выделение Telegram ботов из пула для проектов.

**Задачи:**
1. API для управления пулом ботов:
   - `POST /api/telegram-bots` — регистрация бота админом
   - `GET /api/telegram-bots/available` — список свободных
   - `POST /api/telegram-bots/{id}/allocate` — привязка к проекту
2. Расширить Zavhoz инструментами:
   - `allocate_telegram_bot(project_id)` — выделяет бота из пула
   - `release_telegram_bot(project_id)` — освобождает при удалении проекта
3. Интеграция в DevOps flow:
   - Если проект требует `TELEGRAM_BOT_TOKEN` и нет в secrets — запросить из пула или у пользователя

---

### RAG с Embeddings (Hybrid Search)

**Priority:** MEDIUM  
**Status:** TODO  
**Source:** RAG_PLAN.md, phase5-6-integration-rag.md

Полноценная RAG система с embeddings вместо текущего stub'а.

**Задачи:**
1. Включить pgvector в Postgres
2. Добавить таблицы: `rag_documents`, `rag_chunks` с embeddings
3. Реализовать ingestion pipeline:
   - Индексировать project specs, README, ADRs
   - Chunking: 512 tokens, 10% overlap
4. Hybrid search: FTS + vector retrieval
5. Scopes: `docs`, `code`, `history`, `logs`

**Детали:**
- Embedding model: OpenAI text-embedding-3-small, 512 dimensions
- Token budget: top_k=5, max_tokens=2000, min_similarity=0.7

---

### API Authentication Middleware

**Priority:** MEDIUM  
**Status:** TODO  
**Source:** mvp_gap_analysis.md

API endpoints не защищены аутентификацией (кроме x-telegram-id header).

**Задачи:**
1. Добавить authentication middleware в FastAPI
2. API key validation для внешних сервисов
3. Rate limiting per API key

---

### Scheduler Distributed Locks

**Priority:** LOW  
**Status:** TODO  
**Source:** mvp_gap_analysis.md

Race conditions в scheduler tasks при multiple instances.

**Задачи:**
1. Redis distributed locks для background tasks
2. Lock acquisition с timeout
3. Graceful fallback если lock не получен

---

## Future Improvements

### DevOps: Add Rollback Capability

**Priority:** LOW

Поддержка отката к предыдущему успешному деплою если текущий не проходит health checks.

---

### OpenTelemetry Integration

**Priority:** MEDIUM  
**Prerequisites:** Structured Logging Implementation (DONE)

Distributed tracing для визуализации flow запросов через все микросервисы.

**Преимущества:**
- Видеть весь путь запроса через все сервисы с временными метками
- Автоматическая связь логов через trace_id
- Flamegraph для поиска bottleneck'ов

**Стек:** Grafana Tempo (traces) + Grafana Loki (logs) + Prometheus (metrics)

---

### Cost Tracking

**Priority:** LOW

Отслеживание расходов на LLM:
- Логировать tokens per request
- Агрегировать по проектам
- Алерты при превышении бюджета

---

### Human Escalation

**Priority:** MEDIUM

Когда просить помощи у человека:
- Агент застрял > N итераций
- Ошибка без recovery
- Финансовые решения (покупка домена, сервера)
- Merge в main с breaking changes

**Частично реализовано:** `needs_human_approval` flag в `OrchestratorState` и max iterations в Engineering subgraph.

---

### CLI Interface

**Priority:** LOW

Альтернативный интерфейс помимо Telegram:
```bash
orchestrator new "Weather bot with notifications"
orchestrator status
orchestrator deploy
```

---

## Technical Debt / Optimizations

### MemorySaver Eviction (LangGraph)

**Priority:** LOW (Defer until memory becomes an issue)

`MemorySaver` хранит все checkpoints графа в RAM без eviction. При ~2.7KB на checkpoint это ~20MB/неделю при активном использовании.

**Опции:**
1. Periodic cleanup task (`graph.checkpointer.storage.clear()`)
2. Custom TTLMemorySaver wrapper с LRU eviction
3. Migrate to PostgresSaver (requires direct DB access from langgraph)

---

### Singleton HTTP Client (Telegram Bot)

**Priority:** LOW (Defer until high load)

Использовать Singleton `httpx.AsyncClient` в Telegram Bot для переиспользования SSL-соединений.

---

## Completed (Reference)

### Infrastructure & Core
- ✅ **Sysbox Installation** — Installed on dev machine for nested Docker
- ✅ **Worker Docker Image** — `coding-worker:latest` with Factory.ai Droid CLI
- ✅ **Worker Spawner** — Redis pub/sub microservice for Docker isolation
- ✅ **Scheduler Service** — Moved background tasks (github_sync, server_sync, health_checker) out of API

### Dynamic ProductOwner Architecture
- ✅ **Intent Parser** — gpt-4o-mini for cheap intent classification and capability selection
- ✅ **Capability Registry** — Dynamic tool loading by capability groups
- ✅ **PO Agentic Loop** — Iterative tool execution with user confirmation
- ✅ **Session Management** — Redis-based session locking (PROCESSING/AWAITING states)

### Engineering Pipeline
- ✅ **Engineering Subgraph** — Analyst → Developer → Tester with rework loop
- ✅ **Developer Validation** — Commit SHA validation, max iterations guard

### DevOps Pipeline
- ✅ **DevOps Subgraph** — LLM-based env analysis, secret classification
- ✅ **Secret Resolution** — Auto-generates infra secrets, requests user secrets

### Multi-tenancy
- ✅ **User Propagation** — `telegram_user_id` and `user_id` through all graph nodes
- ✅ **Project Filtering** — `owner_only` filter for project lists

### GitHub Integration
- ✅ **GitHub App** — Auto-detects org, creates repos with correct permissions
- ✅ **Architect Node** — Creates repos, saves repository_url to project

---

## Archived (Outdated/Superseded)

<details>
<summary>Старые задачи из фаз 0-3 (заменены Dynamic PO архитектурой)</summary>

Следующие задачи были частью оригинального фазового плана, но архитектура изменилась:

- **Фаза 0: Поднять инфраструктуру** — Базовые setup инструкции, не backlog item
- **Фаза 0: SOPS + AGE для секретов** — Не реализовано, секреты хранятся в project.config.secrets через API
- **Фаза 1: Минимальный Telegram → LangGraph flow** — Реализовано через telegram_bot + langgraph сервисы
- **Фаза 2: Parallel Developer Node** — Заменено на Engineering subgraph с rework loop
- **Фаза 2: Reviewer Node** — Не реализовано, review через Engineering subgraph
- **Фаза 3: DevOps Node + prod_infra** — Заменено на DevOps subgraph с LLM-анализом секретов
- **Advanced Model Management & Dashboard** — Частично реализовано через LLM factory и agent_configs в БД

**Архитектурные решения сохранены в commit history (планы удалены при cleanup 2025-12-30):**
- Dynamic ProductOwner: Intent Parser + Capability Registry + Agentic Loop
- Engineering Subgraph: Architect → Preparer → Developer → Tester
- DevOps Subgraph: EnvAnalyzer (LLM) → SecretResolver → Deployer
- Session Management: Redis-based locks with AWAITING/PROCESSING states

</details>

