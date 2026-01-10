# Migration Plan / План миграции

Поэтапный план приведения кодовой базы к целевой архитектуре.

## Goals / Цели

1. **Чёткие контракты** — все очереди типизированы Pydantic схемами
2. **Единая терминология** — см. [GLOSSARY.md](./GLOSSARY.md)
3. **Тестируемость** — каждый сервис можно тестировать изолированно
4. **Минимум в shared** — только то что реально нужно всем

---

## Phase 1: Contracts (Контракты)

**Цель:** Создать типизированные схемы для всех очередей.

### 1.1 Создать структуру контрактов

```bash
mkdir -p shared/contracts/queues
```

**Файлы:**
- [ ] `shared/contracts/__init__.py`
- [ ] `shared/contracts/base.py` — BaseMessage, BaseResult, QueueMeta
- [ ] `shared/contracts/events.py` — ProgressEvent
- [ ] `shared/contracts/queues/__init__.py`
- [ ] `shared/contracts/queues/engineering.py`
- [ ] `shared/contracts/queues/deploy.py`
- [ ] `shared/contracts/queues/scaffolder.py`
- [ ] `shared/contracts/queues/provisioner.py`
- [ ] `shared/contracts/queues/ansible_deploy.py`
- [ ] `shared/contracts/queues/worker.py`

### 1.2 Создать типизированный queue client

- [ ] `shared/queue_client.py` — обёртка над Redis с валидацией

```python
async def publish_message(queue: str, message: BaseMessage) -> str:
    """Publish validated message to queue."""
    data = message.model_dump_json()
    return await redis.xadd(queue, {"data": data})

async def consume_messages(queue: str, consumer: str) -> AsyncIterator[dict]:
    """Consume messages with automatic parsing."""
    ...
```

### 1.3 Contract tests

- [ ] `shared/contracts/tests/test_schemas.py` — валидация схем
- [ ] `services/api/tests/integration/test_queue_contracts.py` — проверка что API эндпоинты соответствуют схемам

**Acceptance criteria:**
- Все схемы проходят валидацию
- Примеры сообщений из текущего кода валидируются новыми схемами

---

## Phase 2: Terminology (Терминология)

**Цель:** Переименовать сущности согласно [GLOSSARY.md](./GLOSSARY.md).

### 2.1 Переименование сервисов

| Было | Стало | Тип изменения |
|------|-------|---------------|
| `engineering-worker` | `engineering-consumer` | docker-compose service name |
| `deploy-worker` | `deploy-consumer` | docker-compose service name |
| `infrastructure-worker` | `infra-consumer` | docker-compose service name |
| `workers-spawner` | `worker-manager` | service + directory |
| `universal-worker` | `worker-base` | image name |

### 2.2 Переименование очередей

| Было | Стало |
|------|-------|
| `cli-agent:commands` | `worker:commands` |
| `cli-agent:responses` | `worker:responses` |

### 2.3 Переименование в коде

- [ ] `agent_id` → `worker_id` в worker командах
- [ ] Комментарии и docstrings
- [ ] Логи (structlog events)

**Стратегия:**
1. Добавить алиасы для обратной совместимости
2. Обновить producers
3. Обновить consumers
4. Удалить алиасы

---

## Phase 3: CLI Extraction (Выделение CLI)

**Цель:** Вынести orchestrator CLI из shared в отдельный пакет.

### 3.1 Текущая структура

```
shared/
└── cli/                    # 1,968 LOC
    ├── src/orchestrator/
    │   ├── commands/       # project, deploy, engineering, answer
    │   ├── models/
    │   └── ...
    └── tests/
```

### 3.2 Целевая структура

```
packages/
└── orchestrator-cli/
    ├── pyproject.toml
    ├── src/orchestrator/
    └── tests/

# worker-base Dockerfile:
RUN pip install /packages/orchestrator-cli
```

### 3.3 Шаги

- [ ] Создать `packages/orchestrator-cli/pyproject.toml`
- [ ] Перенести код из `shared/cli/`
- [ ] Обновить `worker-base` Dockerfile
- [ ] Обновить импорты в shared (если есть)
- [ ] Удалить `shared/cli/`

---

## Phase 4: Testing Strategy (Стратегия тестирования)

### 4.1 Уровни тестов

```
                    ┌──────────┐
                    │   E2E    │  1-2 теста: User → Bot created
                   ─┴──────────┴─
                 ┌────────────────┐
                 │  Integration   │  Service + Redis + DB
                ─┴────────────────┴─
              ┌──────────────────────┐
              │     Contract         │  Queue schemas, API endpoints
             ─┴──────────────────────┴─
           ┌────────────────────────────┐
           │         Unit               │  Pure functions, business logic
          ─┴────────────────────────────┴─
```

### 4.2 Unit Tests

**Где:** `services/{service}/tests/unit/`

**Что тестируем:**
- Бизнес-логика без I/O
- Валидация данных
- Трансформации

**Как:**
- Все зависимости мокаются
- Быстрые (< 1ms на тест)
- Можно запускать без Docker

```bash
make test-{service}-unit
```

### 4.3 Contract Tests

**Где:** `shared/contracts/tests/`, `services/api/tests/contract/`

**Что тестируем:**
- Схемы сообщений для очередей
- API endpoints существуют и принимают правильные данные
- Response schemas соответствуют ожидаемым

**Как:**
- Проверка против реальных примеров
- Проверка backward compatibility

```bash
make test-contracts
```

### 4.4 Service Tests (Integration)

**Где:** `services/{service}/tests/integration/`

**Что тестируем:**
- Сервис + его зависимости (Redis, DB)
- Публикация/потребление сообщений
- API endpoints с реальной DB

**Как:**
- Docker Compose для зависимостей
- Изолированная test DB
- Можно использовать реальный Redis

```bash
make test-{service}-integration
```

### 4.5 E2E Tests

**Где:** `tests/e2e/`

**Что тестируем:**
- Полный флоу от User до результата
- Для MVP: "Пользователь просит бота" → "Бот создан и работает"

**Как:**
- Все сервисы запущены
- Реальные внешние сервисы (GitHub test org, test Telegram)
- Может использовать реальный LLM (1 запрос на тест — OK)

```bash
make test-e2e
```

### 4.6 Что тестировать для каждого сервиса

| Сервис | Unit | Contract | Integration | E2E |
|--------|------|----------|-------------|-----|
| api | Models, validation | API schemas | CRUD operations | - |
| engineering-consumer | - | Message parsing | Queue → Subgraph | Part of flow |
| deploy-consumer | - | Message parsing | Queue → Subgraph | Part of flow |
| scaffolder | Git operations | Message parsing | Queue → GitHub | Part of flow |
| infra-consumer | Ansible parsing | Message parsing | Queue → Result key | - |
| worker-manager | Container config | Command parsing | Create/destroy | - |
| telegram-bot | Handlers | - | Bot → API | Entry point |
| scheduler | Task logic | - | Jobs execution | - |

---

## Phase 5: Shared Cleanup

**Цель:** Минимизировать shared/, оставить только общее.

### 5.1 Текущее содержимое shared/

```
shared/
├── models/          # 697 LOC  — ORM модели (нужно всем)
├── schemas/         # 786 LOC  — Pydantic схемы (частично)
├── cli/             # 1,968 LOC — CLI для агентов (вынести)
├── clients/         # 999 LOC  — HTTP клиенты (раскидать по сервисам)
├── config.py        # 135 LOC  — Base settings (оставить)
├── logging_config.py# 123 LOC  — Structlog (оставить)
├── queues.py        # 105 LOC  — Queue constants (объединить с contracts)
├── redis_client.py  # ~150 LOC — Redis wrapper (оставить)
└── tests/
```

### 5.2 Целевая структура shared/

```
shared/
├── models/           # ORM модели
├── contracts/        # Pydantic схемы для очередей (NEW)
├── config.py         # Base settings
├── logging_config.py # Structlog setup
└── redis_client.py   # Redis wrapper с типизацией
```

### 5.3 Миграция клиентов

| Клиент | Куда переносим |
|--------|----------------|
| `clients/github.py` | `services/langgraph/src/clients/` |
| `clients/time4vps.py` | `services/scheduler/src/clients/` |
| `clients/embedding.py` | `services/api/src/clients/` |

### 5.4 Миграция схем

| Схема | Куда |
|-------|------|
| `schemas/deployment_jobs.py` | `shared/contracts/queues/ansible_deploy.py` |
| `schemas/worker_events.py` | `shared/contracts/events.py` |
| `schemas/project_spec.py` | `services/langgraph/src/schemas/` |
| Остальные | По сервисам |

---

## Execution Order

### Week 1: Foundation
1. [ ] Создать `shared/contracts/` структуру
2. [ ] Написать base schemas
3. [ ] Написать contract tests

### Week 2: Queues
4. [ ] Типизировать все очереди
5. [ ] Добавить валидацию в consumers
6. [ ] Обновить producers

### Week 3: Terminology
7. [ ] Переименовать сервисы в docker-compose
8. [ ] Переименовать очереди (с backwards compat)
9. [ ] Обновить документацию

### Week 4: Cleanup
10. [ ] Вынести CLI в отдельный пакет
11. [ ] Разнести clients по сервисам
12. [ ] Удалить deprecated код

### Week 5: Testing
13. [ ] Добавить contract tests для всех очередей
14. [ ] Добавить integration tests для каждого consumer
15. [ ] Написать 1-2 E2E теста для MVP

---

## Success Criteria

### Contracts
- [ ] Все очереди имеют Pydantic схемы
- [ ] Все сообщения валидируются при отправке/получении
- [ ] Contract tests проходят

### Terminology
- [ ] Код использует единую терминологию
- [ ] Документация обновлена
- [ ] Нет "worker" для consumers

### Testing
- [ ] Каждый сервис имеет unit tests
- [ ] Contract tests покрывают все очереди
- [ ] Есть минимум 1 E2E тест

### Shared
- [ ] CLI вынесен в отдельный пакет
- [ ] shared/ содержит только общий код
- [ ] Нет дублирования между сервисами

---

## Risks & Mitigations

| Риск | Митигация |
|------|-----------|
| Сломать production при переименовании очередей | Dual-read: consumers читают и старые и новые очереди |
| Потерять данные при миграции | Миграция без даунтайма: сначала новое, потом удаляем старое |
| Регрессии от рефакторинга | Contract tests ловят breaking changes |
| Долго тянуть миграцию | Делаем инкрементально, каждая фаза — самодостаточна |
