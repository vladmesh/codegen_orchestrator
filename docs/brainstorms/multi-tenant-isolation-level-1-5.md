---
id: bs-8437c4b3
status: triaged
title: "Multi-Tenant Isolation (Level 1.5)"
created_at: 2026-03-07T12:37:05.009742Z
---

# Brainstorm: Уровни изоляции в мультитенантной архитектуре

> **Дата**: 2026-03-05
> **Контекст**: Оркестратор будет использовать единую БД (эпики, таски, проекты) как для собственной разработки, так и для клиентских проектов. Вопрос: где на шкале "всё в одной куче" ↔ "полная изоляция per-project" находится оптимум?
> **Status**: triaged

---

## Current State

### Что есть сейчас

```
Одна PostgreSQL БД (orchestrator)
  tables: projects, tasks, servers, api_keys, agent_configs, allocations, secrets

Один LangGraph сервис
  PO agent (per-user thread через telegram_id)
  Engineering subgraph (shared)
  DevOps subgraph (shared)

Один Redis
  streams: po:input, po:response, engineering:queue, deploy:queue, ...
  checkpointer data

Одна сеть (internal + worker)
  Все сервисы видят друг друга
  Воркеры изолированы в codegen_worker (после #22)
```

### Что планируем добавить

- Task Store в БД (эпики, work items, gates) — для разработки самого оркестратора
- Тот же Task Store будет использоваться оркестратором для клиентских проектов
- Потенциально: несколько пользователей, несколько проектов параллельно

---

## Шкала изоляции

### Level 0: Всё в одной куче (чуть больше чем сейчас)

```
Один сервер, одна БД, одна сеть, один LangGraph
Все пользователи → один PO agent instance
Все проекты → одна таблица projects, tasks, etc.
Таски по разработке оркестратора → та же таблица tasks
Изоляция: WHERE owner_id = X в SQL запросах
```

- (+) Минимум кода, минимум инфры
- (+) Максимально дешево (один VPS)
- (+) Простой деплой, простой бэкап
- (-) Один баг в WHERE = утечка данных между юзерами
- (-) Один OOM/crash = падает всё для всех
- (-) Промпты PO видят контекст всех проектов (если не фильтровать)
- (-) Нет возможности дать юзеру "свой" конфиг агентов
- (-) Таски по оркестратору мешаются с клиентскими

**Кому подходит:** solo-use, прототип, MVP. Мы примерно здесь.

### Level 1: Логическая изоляция (Row-Level Security + tenant_id)

```
Одна БД, но:
  - RLS policy на всех таблицах: current_setting('app.tenant_id')
  - Каждый API запрос → SET app.tenant_id = X
  - Отдельный "системный" tenant для задач по оркестратору
  - Redis streams: prefix per tenant (tenant:{id}:engineering:queue)

Один LangGraph, но:
  - PO thread per user (уже есть)
  - Agent configs per tenant (agent_configs.tenant_id)
  - Промпты могут различаться per tenant
```

- (+) Изоляция данных на уровне БД (не приложения)
- (+) Один инстанс всего — дешево
- (+) RLS проверяется PostgreSQL, не приложением → меньше шансов забыть WHERE
- (+) Можно дать разным тенантам разные agent_configs
- (-) RLS не защищает от superuser/migration ошибок
- (-) Shared compute: тяжелый проект одного юзера тормозит всех
- (-) Один Redis, один LangGraph — bottleneck при масштабировании
- (-) Бэкапы всё ещё общие (нельзя восстановить одного тенанта)

**Кому подходит:** 2-10 пользователей, бета-тест, early adopters.

### Level 2: Изолированные схемы/БД, общий compute

```
PostgreSQL:
  - database: orchestrator_system (таски по разработке, конфиги)
  - database: tenant_{id} (проекты, задачи, секреты юзера)
  ИЛИ schemas: system, tenant_alice, tenant_bob

Redis:
  - Общий, но keyspace isolation (db 0 = system, db 1-15 = tenants)
  ИЛИ отдельные Redis instances per tenant (docker containers)

LangGraph:
  - Один процесс, но checkpointer per tenant DB
  - Thread isolation уже есть
```

- (+) Утечка между тенантами почти невозможна (разные databases)
- (+) Можно бэкапить/восстанавливать тенанта отдельно
- (+) Миграции системной БД не ломают tenant БД и наоборот
- (+) Один LangGraph, один API — всё ещё дешево
- (-) Нужен routing layer: по tenant_id выбирать database
- (-) Миграции нужно накатывать на N databases (или lazy при первом обращении)
- (-) Redis db 0-15 = максимум 16 тенантов (если не отдельные instances)
- (-) Compute всё ещё общий

**Кому подходит:** 10-50 пользователей, платный продукт с гарантиями.

### Level 3: Изолированный compute, shared infrastructure

```
Per-tenant:
  - Свой LangGraph процесс (или container)
  - Своя database (в shared PostgreSQL кластере)
  - Свой Redis keyspace
  - Свои agent configs, промпты, модели

Shared:
  - PostgreSQL кластер (физический)
  - Redis кластер
  - API gateway / proxy
  - Billing, auth, admin
  - Worker farm (воркеры и так изолированы)
```

- (+) Crash одного LangGraph не роняет других
- (+) Можно масштабировать LangGraph per tenant (тяжелый юзер → больше ресурсов)
- (+) Полная изоляция данных И compute
- (+) Можно давать разные версии оркестратора разным тенантам (canary)
- (-) N контейнеров LangGraph = N × 200-500 MB RAM
- (-) Нужен routing proxy (по tenant → к правильному LangGraph)
- (-) Деплой обновлений = rolling update N контейнеров
- (-) Мониторинг и логи: N источников вместо одного

**Кому подходит:** 50+ пользователей, enterprise, SLA.

### Level 4: Полная изоляция (per-tenant stack)

```
Per-tenant (или per-project):
  - Свой VPS / VM / namespace
  - Свой PostgreSQL
  - Свой Redis
  - Свой LangGraph + API + worker-manager
  - Свой Caddy + домен

Shared:
  - Auth proxy / API gateway (маршрутизация по tenant)
  - Billing & metering
  - Control plane (provisioning, updates, monitoring)
```

- (+) Абсолютная изоляция: один тенант = одна машина
- (+) Тенант может кастомизировать всё
- (+) Security: компрометация одного тенанта не затрагивает других
- (+) Compliance: данные тенанта на конкретном сервере/в конкретной юрисдикции
- (-) Стоимость: VPS per tenant (минимум €5-10/мес per tenant)
- (-) Обновления: нужен fleet management (Ansible/Terraform на N серверов)
- (-) Мониторинг: N отдельных стеков
- (-) Cold start: новый тенант = развернуть полный стек (минуты)

**Кому подходит:** enterprise on-prem, regulated industries, "white label".

---

## Ортогональные оси изоляции

Изоляция — не одномерная шкала. Разные компоненты можно изолировать на разных уровнях:

| Компонент | Level 0 | Level 1 | Level 2 | Level 3 | Level 4 |
|-----------|---------|---------|---------|---------|---------|
| **Данные (projects, tasks)** | shared table | RLS | separate DB | separate DB | separate DB + server |
| **Секреты** | shared table | RLS | separate DB | separate DB | separate vault |
| **LLM контекст (промпты)** | shared | per-tenant config | per-tenant config | separate process | separate stack |
| **Compute (LangGraph)** | shared | shared | shared | per-tenant | per-tenant |
| **Worker containers** | shared host | shared host | shared host | worker farm | per-tenant farm |
| **Очереди (Redis)** | shared | prefixed | separate db/instance | separate instance | separate server |
| **Backups** | all-or-nothing | all-or-nothing | per-tenant | per-tenant | per-tenant |
| **Networking** | shared | shared | shared | isolated | isolated |

**Ключевой инсайт**: не обязательно быть на одном уровне по всем осям. Можно иметь Level 2 для данных, Level 1 для compute, и Level 3 для воркеров. Это даёт оптимальный баланс.

---

## Специфичный вопрос: Task Store для оркестратора vs для клиентов

Планируемый Task Store (эпики, work items, gates) будет использоваться двояко:

1. **Разработка оркестратора** (мета): наш бэклог, наши эпики, наши планы
2. **Клиентские проекты**: задачи юзера, декомпозиция его запросов

### Вариант A: Одна модель, tenant isolation

```python
class WorkItem(Base):
    tenant_id: str  # "system" для оркестратора, user_id для клиентов
    project_id: int | None
    title: str
    ...
```

- (+) Dogfooding: один код, одна модель, одни миграции
- (+) Оркестратор разрабатывается тем же инструментом что и клиентские проекты
- (-) Миграции задевают всех (добавили колонку для системных нужд → все тенанты получили)
- (-) Системные таски могут случайно попасть к клиенту (если баг в фильтрации)

### Вариант B: Разные модели, разные таблицы

```python
# System backlog (как сейчас, markdown или отдельная таблица)
class SystemWorkItem(Base):  # system_work_items table
    ...

# Client task store
class ClientWorkItem(Base):  # client_work_items table
    tenant_id: str
    ...
```

- (+) Физическое разделение — невозможно перепутать
- (+) Можно менять schema системных тасков не трогая клиентские
- (-) Два набора миграций, два набора API
- (-) Дублирование логики (если модели похожи)

### Вариант C: Разные databases (Level 2 для этой конкретной задачи)

```
database: orchestrator_system
  tables: system_work_items, system_epics, ...

database: orchestrator_tenants (или per-tenant DBs)
  tables: work_items, epics, projects, tasks, ...
```

- (+) Лучшее из обоих миров: одна модель, но физическая изоляция
- (+) System DB можно бэкапить/мигрировать отдельно
- (-) Нужен multi-database routing в SQLAlchemy (не сложно, но дополнительный код)

---

## Продуктовый взгляд: цена vs безопасность vs удобство

### Стоимость на 10 пользователей (грубая оценка)

| Level | Серверы | Стоимость/мес | Ops overhead |
|-------|---------|---------------|--------------|
| 0 | 1 VPS | €10-20 | минимальный |
| 1 | 1 VPS | €10-20 | +RLS setup |
| 2 | 1 VPS (побольше) | €20-40 | +multi-DB routing |
| 3 | 1 VPS + worker farm | €60-100 | +container orchestration |
| 4 | 10 VPS | €100-200 | +fleet management |

### Что реально важно пользователям

1. **Мои данные не видны другим** — Level 1+ решает
2. **Чужой проект не может положить мой** — Level 3+ решает
3. **Мои секреты в безопасности** — отдельный вопрос (encryption at rest, не зависит от уровня)
4. **Я могу кастомизировать агентов** — Level 1+ решает (per-tenant config)
5. **Стабильная работа** — зависит от resource limits больше чем от isolation

### Что реально важно нам (как оператору)

1. **Простота деплоя** — Level 0-2 сильно проще
2. **Бэкапы и восстановление** — Level 2+ позволяет per-tenant
3. **Масштабирование** — Level 2 масштабируется вертикально, Level 3+ горизонтально
4. **Стоимость** — Level 0-2 = один сервер, Level 3+ = линейный рост

---

## Рекомендация: прагматичный путь

### Сейчас (MVP, 1 пользователь): Level 0

Не менять ничего. Текущая архитектура работает. Единственное — системный tenant_id для отделения своих тасков от клиентских.

### Ближайшее будущее (2-5 пользователей): Level 1.5

- RLS на критичных таблицах (projects, tasks, secrets)
- Отдельная database для системных данных оркестратора (Level 2 по оси "данные")
- Redis prefix isolation (Level 1 по оси "очереди")
- Shared LangGraph (Level 0 по оси "compute")
- Worker isolation уже есть (Level ~2 по оси "workers")

Это даёт достаточную изоляцию при минимальных затратах.

### Когда появится платёж (5-20 пользователей): Level 2

- Per-tenant databases (или переход с RLS на separate DBs)
- Per-tenant Redis instances (Docker containers, не отдельные серверы)
- Agent configs per tenant с возможностью кастомизации
- Worker farm (уже планируется в worker-db-isolation Phase 2)

### Когда появится enterprise (50+): Level 3

- Per-tenant LangGraph containers
- Routing proxy
- Per-tenant worker farms
- SLA и мониторинг per tenant

### Level 4 — только если потребуется

On-prem, compliance, white-label. Не проектировать заранее.

---

## Конкретные решения на сегодня

### 1. Task Store: tenant_id с первого дня

Когда будем делать Task Store в БД — добавлять `tenant_id` сразу. Значение `"system"` для задач по разработке оркестратора, `user_{telegram_id}` для клиентских. Это Level 0 → Level 1 переход с минимальными затратами.

### 2. Не смешивать системную и клиентскую базу

Использовать вариант C — отдельная database для системных данных. SQLAlchemy поддерживает binds (multiple databases) нативно. Клиентские данные — в основной `orchestrator` database (с tenant_id). Системные (наш бэклог, наши конфиги) — в `orchestrator_system`.

### 3. Проектировать API с tenant context

Каждый API endpoint должен работать в контексте tenant (уже частично есть через `X-Telegram-ID`). Подготовка к RLS — когда понадобится, достаточно будет добавить policy в PostgreSQL.

### 4. Redis: prefix сразу

Вместо `engineering:queue` → `tenant:{id}:engineering:queue`. Системные очереди: `system:...`. Это бесплатно по стоимости и подготавливает к Level 2.

---

## Action Items

- → idea (added to backlog Ideas): "RLS policies на PostgreSQL для multi-tenant"
- → idea (added to backlog Ideas): "Redis key prefix isolation (tenant:{id}:*)"
- → idea (merged into existing "Task Store" idea): "tenant_id в модель WorkItem при создании"
- → idea (added to backlog Ideas): "Отдельная database для системных данных оркестратора"
- → backlog #30 (Multi-user Isolation Fix — уже покрывает часть вопросов Level 1)