# Backlog

## Urgent Features

- [ ] **Implement `update_project` tool for Product Owner**
  - **Problem**: Current `activate_project` logic attempts to "guess" the repository URL but fails to save it persistently if the project was created before the fix. This leaves "legacy" projects (like `hello-world-bot`) in a state where deployment is impossible because DevOps subgraph doesn't know where to pull code from. PO sees the issue but has no tool to fix the data.
  - **Proposed Solution**: Create a dedicated `update_project(project_id, repository_url=None, status=None)` tool in `services/langgraph/src/tools/projects.py`. Add it to `project_management` capability.
  - **Why**: Allows recovering stuck projects without manual database intervention.

## Technical Debt

- [ ] **Refactor `check_deploy_readiness`**
  - **Context**: Currently `check_deploy_readiness` performs a static check against `project.config.secrets`. This logic is partially redundant with the DevOps subgraph's `env_analyzer` (LLM-based) and `readiness_check`.
  - **Goal**: Align validation logic so we don't have two sources of truth for what "ready" means.

## Future Improvements

- [ ] **DevOps: Add Rollback Capability**
  - Support rolling back to previous successful deployment if current one fails health checks.

## Фаза 0: Foundation

### Поднять инфраструктуру

**Status:** TODO
**Priority:** HIGH

Базовая инфраструктура для разработки оркестратора.

**Tasks:**
- [ ] `cp .env.example .env` и заполнить переменные
- [ ] `make build && make up`
- [ ] `make migrate` — создать таблицы в БД
- [ ] Проверить что API отвечает на `/health`

---

### Установить Sysbox на сервер оркестратора

**Status:** DONE
**Priority:** HIGH

Для параллельных workers нужен Sysbox runtime.

**Tasks:**
- [x] Скачать и установить Sysbox CE
- [x] Проверить `docker info | grep sysbox`
- [x] Протестировать запуск nested Docker

**Docs:** https://github.com/nestybox/sysbox

---

### Настроить SOPS + AGE для секретов

**Status:** TODO
**Priority:** HIGH

Шифрование secrets.yaml для хранения токенов и ключей.

**Tasks:**
- [ ] Установить SOPS и AGE
- [ ] Сгенерировать AGE ключ
- [ ] Создать secrets.yaml с тестовыми данными
- [ ] Проверить шифрование/дешифрование

---

## Фаза 1: Вертикальный слайс

### Минимальный Telegram → LangGraph flow

**Status:** TODO
**Priority:** HIGH

Пользователь пишет в Телеграм, получает ответ от LangGraph.

**Tasks:**
- [ ] Создать Telegram бота через @BotFather
- [ ] Прописать токен в `.env`
- [ ] Реализовать передачу сообщений из бота в LangGraph
- [ ] Реализовать отправку ответа обратно в Телеграм

**Open questions:**
- Как хранить thread_id для пользователя? (Redis? Postgres?)

---

### Brainstorm → Architect flow

**Status:** DONE
**Priority:** MEDIUM

Брейнсторм создаёт спецификацию, Архитектор генерирует проект.

**Tasks:**
- [x] Реализовать brainstorm node с LLM
- [x] Определить формат project_spec
- [x] Реализовать architect node с Factory.ai
- [x] GitHub App для создания репозиториев

---

### Zavhoz: выдача ресурсов

**Status:** DONE
**Priority:** MEDIUM

Завхоз выдаёт handles для ресурсов, не раскрывая секреты LLM.

**Tasks:**
- [x] Модель Resource в API (уже есть базовая)
- [x] Эндпоинты: allocate, get, list
- [ ] Интеграция с SOPS для чтения реальных секретов
- [x] Tool для LangGraph: request_resource

---

## Фаза 2: Параллельные Workers

### Worker Docker Image

**Status:** DONE
**Priority:** MEDIUM

Образ с git, gh CLI, Factory.ai для выполнения coding tasks.

**Tasks:**
- [x] Dockerfile на базе Ubuntu 22.04
- [x] Установить git, gh, Factory.ai Droid CLI
- [x] Скрипт execute_task.sh
- [x] Протестировать с Sysbox runtime

---

### Worker Spawner Microservice

**Status:** DONE
**Priority:** HIGH

Микросервис для изоляции Docker API от LangGraph.

**Tasks:**
- [x] Redis pub/sub коммуникация
- [x] `worker:spawn` / `worker:result:{id}` каналы
- [x] Docker socket mount
- [x] Client library для LangGraph

---

### Parallel Developer Node

**Status:** TODO
**Priority:** MEDIUM

Узел графа для параллельного запуска coding workers.

**Tasks:**
- [ ] spawn_sysbox_worker function
- [ ] asyncio.gather для параллельного запуска
- [ ] Парсинг результатов (PR URL, статус)
- [ ] Обработка ошибок

---

### Reviewer Node

**Status:** TODO  
**Priority:** MEDIUM

Ревью и merge PR через gh CLI.

**Tasks:**
- [ ] gh pr diff для получения изменений
- [ ] LLM для code review
- [ ] gh pr merge или gh pr comment
- [ ] Логика возврата на доработку

---

## Фаза 3: DevOps Integration

### DevOps Node + prod_infra

**Status:** TODO
**Priority:** LOW

Интеграция с Ansible для деплоя.

**Tasks:**
- [ ] Wrapper над ansible-playbook
- [ ] Обновление services.yml
- [ ] DNS через Cloudflare API
- [ ] Health check после деплоя

**Open questions:**
- Как передать SSH ключ агенту? (через Завхоза?)
- Как обрабатывать ошибки Ansible?

---

## Ideas / Future

### OpenTelemetry Integration

**Status:** BACKLOG  
**Priority:** MEDIUM  
**Prerequisites:** Structured Logging Implementation

Distributed tracing для визуализации flow запросов через все микросервисы.

**Benefits:**
- Видеть весь путь запроса через все сервисы с временными метками
- Автоматическая связь логов через trace_id
- Flamegraph для поиска bottleneck'ов
- Метрики latency/error rate из коробки

**Tasks:**
- [ ] Поднять Grafana Tempo для traces
- [ ] Добавить `opentelemetry-api` и `opentelemetry-sdk` в зависимости
- [ ] Создать `shared/telemetry.py` с setup функцией
- [ ] Auto-instrument FastAPI (одна строка - `FastAPIInstrumentor.instrument_app(app)`)
- [ ] Добавить manual spans в ключевые LangGraph nodes (Zavhoz, Developer, DevOps)
- [ ] Настроить Grafana dashboards для traces
- [ ] Интеграция Tempo с Loki (клик на лог → показать trace)

**Stack:**
- Grafana Tempo (traces storage)
- Grafana Loki (logs storage)
- Prometheus (metrics)
- Unified Grafana UI

**Docs:** https://opentelemetry.io/docs/

---

### Cost Tracking

Отслеживание расходов на LLM.

**Ideas:**
- Логировать tokens per request
- Агрегировать по проектам
- Алерты при превышении бюджета

---

### Human Escalation

Когда просить помощи у человека.

**Triggers:**
- Агент застрял > N итераций
- Ошибка без recovery
- Финансовые решения (покупка домена, сервера)
- Merge в main с breaking changes

---

### Multi-tenancy

Несколько пользователей / проектов.

**Questions:**
- Разные Telegram пользователи = разные threads?
- Изоляция ресурсов между проектами?
- Квоты на LLM usage?

---

### CLI Interface

Альтернативный интерфейс помимо Telegram.

```bash
# Идея
orchestrator new "Weather bot with notifications"
orchestrator status
orchestrator deploy
```

---

---

### Advanced Model Management & Dashboard

**Status:** TODO
**Priority:** MEDIUM

Support for late 2025 SOTA models (gpt-5.2, Gemini 3 Pro, Claude Opus 4.5) and dynamic runtime configuration.

**Tasks:**
- [ ] Database schema for storing Model Configs (provider, model_name, api_key_ref, temperature, prompt_templates).
- [ ] Admin Dashboard (Web UI) for managing these configs at runtime.
- [ ] Dynamic LLM factory that reads from DB instead of envs.
- [ ] Support for high-end models: `gpt-5.2`, `google/gemini-3-pro`, `anthropic/claude-opus-4.5`.

### Refactor: Move background tasks out of API

**Status:** DONE
**Priority:** MEDIUM

API should be a clean CRUD layer. All background polling/monitoring should be in a separate service.

**Tasks:**
- [x] Create `scheduler` or `worker` service in Docker
- [x] Move `health_checker`, `server_sync`, `github_sync` from API
- [x] Remove background task initialization from `api/src/main.py`
- [x] Configure SSH keys and credentials only for the new service

---

### Technical Debt / Optimizations

### MemorySaver Eviction (LangGraph)

**Status:** TODO
**Priority:** LOW (Defer until memory becomes an issue)

`MemorySaver` хранит все checkpoints графа в RAM без eviction. При ~2.7KB на checkpoint это ~20MB/неделю при активном использовании.

**Options:**
1. Periodic cleanup task (`graph.checkpointer.storage.clear()`)
2. Custom TTLMemorySaver wrapper с LRU eviction
3. Migrate to PostgresSaver (requires direct DB access from langgraph)

**Tasks:**
- [ ] Добавить memory stats logging для мониторинга
- [ ] Реализовать периодическую очистку старых threads
- [ ] Или мигрировать на PostgresSaver если нужен persistent state

---

### Singleton HTTP Client (Telegram Bot)

**Status:** TODO
**Priority:** LOW (Defer until high load)

Использовать Singleton `httpx.AsyncClient` в Telegram Bot для переиспользования SSL-соединений.

**Tasks:**
- [ ] Вынести `httpx.AsyncClient` в глобальную переменную или Dependency Injection в `services/telegram_bot`
- [ ] Использовать этот клиент во всех handlers вместо создания нового на каждый запрос
- [ ] Корректно закрывать клиент при shutdown

---

### Fix datetime serialization in worker events forwarding

**Status:** TODO
**Priority:** LOW

Worker events (started, progress, completed, failed) не пересылаются в stream `orchestrator:events` из-за ошибки сериализации datetime.

**Location:** `services/langgraph/src/worker.py:296`

**Root cause:**
```python
# Текущий код
await publish_event(f"worker.{event.event_type}", event.model_dump())
```

`WorkerEvent` содержит поле `timestamp: datetime`. При вызове `model_dump()` datetime остаётся объектом Python, а `json.dumps()` в `RedisStreamClient.publish()` не умеет его сериализовать.

**Impact:**
- Worker events не попадают в `orchestrator:events` stream
- Мониторинг прогресса воркеров через этот stream не работает
- Основной flow НЕ затронут — воркер работает независимо

**Fix:**
```python
# Использовать mode="json" для автоматической конвертации datetime в ISO string
await publish_event(f"worker.{event.event_type}", event.model_dump(mode="json"))
```

**Tasks:**
- [ ] Заменить `event.model_dump()` на `event.model_dump(mode="json")` в `worker.py:296`
- [ ] Проверить другие места где используется `model_dump()` перед JSON сериализацией

---

## Done

- **Sysbox Installation** - Installed on dev machine
- **Worker Docker Image** - `coding-worker:latest` with Factory.ai
- **Worker Spawner** - Redis pub/sub microservice
- **Architect Node** - Creates GitHub repos, spawns Factory workers
- **GitHub App Integration** - Auto-detects org, creates repos
- **Brainstorm → Zavhoz → Architect flow** - Tested end-to-end
- **Scheduler Service** - Moved all background tasks out of API into dedicated service

