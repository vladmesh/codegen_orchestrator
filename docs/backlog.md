# Backlog

> Актуально на: 2025-12-31

## Technical Debt (Активная работа)

### CLI Status --follow Mode (Real-time Event Streaming)

**Priority:** LOW  
**Status:** TODO  
**Location:** `shared/cli/src/orchestrator/commands/engineering.py:92-94`, `deploy.py:92-94`

**Проблема:** Команды `orchestrator engineering status <task_id>` и `orchestrator deploy status <task_id>` поддерживают флаг `--follow`, но он не реализован — выводится заглушка "Note: --follow mode not yet implemented".

**Что должен делать --follow:**
Стримить события из Redis в реальном времени пока задача выполняется:
```bash
$ orchestrator engineering status eng-abc123 --follow
⏳ Engineering task started
📦 Architect: analyzing requirements...
🔨 Developer: implementing feature X...
✅ Task completed successfully
```

**Варианты реализации:**

#### Вариант A: Redis XREAD blocking (рекомендуется)
```python
@app.command()
def status(task_id: str, follow: bool = False):
    if follow:
        r = _get_redis()
        user_id = os.getenv("ORCHESTRATOR_USER_ID")
        stream = f"agent:events:{user_id}"
        last_id = "0"
        
        while True:
            events = r.xread({stream: last_id}, block=5000, count=10)
            for stream_name, entries in events:
                for entry_id, data in entries:
                    event = json.loads(data["data"])
                    if event.get("task_id") == task_id:
                        console.print(format_event(event))
                        if event["type"] in ("completed", "failed"):
                            return
                    last_id = entry_id
```

**Плюсы:** Простая реализация, использует существующий `callback_stream`.
**Минусы:** Фильтрация на клиенте (читаем все события user'а).

#### Вариант B: Dedicated task stream
Создавать отдельный stream для каждой задачи: `task:{task_id}:events`.

**Плюсы:** Точный таргетинг, нет лишнего трафика.
**Минусы:** Нужно менять workers, больше streams в Redis.

#### Вариант C: SSE через API
Добавить endpoint `GET /api/tasks/{task_id}/events` с Server-Sent Events.

**Плюсы:** Работает через HTTP, можно использовать из браузера.
**Минусы:** Дополнительный endpoint, сложнее из CLI.

**Рекомендация:** Вариант A — минимальные изменения, workers уже публикуют в `callback_stream`.

**Связанные файлы:**
- `services/langgraph/src/workers/engineering_worker.py` — публикует события
- `services/langgraph/src/workers/deploy_worker.py` — публикует события
- `shared/queues.py` — константы очередей

---

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

### Engineering Pipeline: TesterNode & Worker Integration

**Priority:** MEDIUM
**Status:** TODO
**Location:** `services/langgraph/src/subgraphs/engineering.py`, `services/langgraph/src/workers/engineering_worker.py`

**Проблема:** TesterNode и engineering_worker — заглушки. Тесты не запускаются реально.

**Текущее состояние:**
- `TesterNode.run()` всегда возвращает `passed=True` без запуска тестов
- `engineering_worker` не интегрирован с `engineering_subgraph`

**Задачи:**
1. Интегрировать `engineering_worker` с `create_engineering_subgraph()`:
   - Вызывать скомпилированный граф с правильным state
   - Пробрасывать результаты обратно в Redis
2. Реализовать запуск тестов в `TesterNode`:
   - Использовать `worker_spawner` для запуска тестов в контейнере
   - Парсить результаты pytest/unittest
   - Извлекать ошибки для передачи в Developer на retry
3. Добавить test configuration в project spec (test command, coverage threshold)

**Связанные файлы:**
- `services/langgraph/src/clients/worker_spawner.py` — существующий spawner
- `services/langgraph/src/nodes/developer.py` — пример использования spawner

---

## Future Improvements (Extracted from archived plans)

### Caddy Reverse Proxy Integration (убрать Port Management)

**Priority:** MEDIUM  
**Status:** TODO  
**Location:** `services/infrastructure-worker/ansible/roles/caddy/`, `services/api/`, `services/langgraph/src/tools/devops_tools.py`

**Проблема:** Сейчас каждый проект деплоится на уникальный порт (8080, 8081, ...). Zavhoz выделяет порты, Ansible открывает их в UFW. Это создаёт:
1. Сложность управления — таблица `allocations` с портами
2. Ugly URLs — `http://server:8080` вместо `https://project.example.com`
3. Ручное управление firewall

**Что уже есть:**
- Caddy установлен на VPS (`ansible/roles/caddy/`)
- Caddyfile генерируется из `services.yml` шаблоном Jinja2
- Автоматический HTTPS через Let's Encrypt

**Что улучшится:**
1. ✅ **Нет port management** — все проекты на 80/443, роутинг по домену
2. ✅ **Автоматический HTTPS** — Caddy получает сертификаты автоматически
3. ✅ **Проще firewall** — только 22/80/443 открыты
4. ✅ **Красивые URL** — `https://myproject.example.com`
5. ✅ **Проще восстановление** — конфиг генерится из БД

**Варианты реализации:**

#### Вариант A: API + Regenerate Caddyfile (рекомендуется)

Source of truth — БД оркестратора. При деплое:
1. Регистрируем сервис в API (`POST /servers/{handle}/services`)
2. SSH на сервер → скрипт тянет конфиг из API → генерирует Caddyfile → `caddy reload`

```python
# Новые API endpoints
GET  /servers/{handle}/services  # список сервисов на сервере
POST /servers/{handle}/services  # регистрация сервиса {project_id, domain, port}
DELETE /servers/{handle}/services/{id}  # удаление при undeploy

# Скрипт на VPS: /opt/caddy/regenerate.sh
curl -s http://orchestrator-api/servers/$(hostname)/services | \
  python3 /opt/caddy/generate_caddyfile.py > /opt/caddy/Caddyfile && \
  docker exec caddy caddy reload --config /etc/caddy/Caddyfile
```

**Плюсы:** Простота, source of truth в БД, восстановление после reinstall.
**Минусы:** Нужен вызов после каждого деплоя.

#### Вариант B: Caddy Admin API (on-the-fly)

Добавлять роуты напрямую через Caddy Admin API без перегенерации файла:

```bash
curl -X POST "http://localhost:2019/config/apps/http/servers/srv0/routes" \
  -d '{"@id": "project-abc", "match": [{"host": ["abc.example.com"]}], ...}'
```

**Плюсы:** Мгновенное применение, нет файлов.
**Минусы:** State теряется при рестарте Caddy (нужен persistent config).

#### Вариант C: Traefik вместо Caddy

Traefik читает Docker labels автоматически — не нужно ничего вызывать:

```yaml
# В docker-compose проекта
labels:
  - "traefik.http.routers.myproject.rule=Host(`myproject.example.com`)"
```

**Плюсы:** Полная автоматика, zero config при деплое.
**Минусы:** Нужно менять provisioning, service-template, переучиваться.

**Рекомендация:** Вариант A — минимальные изменения, Caddy уже установлен.

**Задачи:**
1. Добавить модель `ServiceDeployment` в API (или расширить существующую)
2. Добавить endpoints для управления сервисами на сервере
3. Создать скрипт генерации Caddyfile из API на VPS
4. Модифицировать `deploy_project.yml` — вызывать regenerate после compose up
5. Убрать port allocation из Zavhoz (или сделать optional)
6. Добавить `domain` в project config (или генерировать из project_name)

---

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

### Docker Python SDK Migration

**Priority:** LOW  
**Status:** TODO  
**Location:** `services/workers-spawner/`

**Текущее состояние:** Workers-spawner использует subprocess (`docker run`, `docker exec`, etc.) для управления контейнерами.

**Зачем мигрировать на Python Docker SDK:**
1. **Real-time log streaming** — `container.logs(stream=True)` вместо polling
2. **Docker events subscription** — слушать `container.start`, `container.die` для instant status updates
3. **Типизированные объекты** — `Container`, `Image` вместо json parsing
4. **Встроенная обработка ошибок** — `docker.errors.NotFound`, `docker.errors.APIError`
5. **Проще работа с volumes/networks** — SDK абстрагирует сложные mount конфиги

**Не нужно для MVP:** Subprocess достаточно для run → exec → rm цикла.

---

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

