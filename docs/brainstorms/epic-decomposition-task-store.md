---
id: bs-42566a84
status: triaged
title: "Epic Decomposition & Task Store"
created_at: 2026-03-07T12:37:04.730246Z
---

# Brainstorm: Декомпозиция больших задач (эпики)

> **Дата**: 2026-03-05
> **Контекст**: Текущий `/plan` хорош для задач на 3-7 шагов в одном репо. Но крупные продуктовые фичи (cross-repo, многонедельные, с промежуточными E2E/аудитами) не ложатся в одну задачу. Нужен уровень выше — аналог эпиков.
> **Status**: triaged

---

## Current State

### Что есть сейчас (dev pipeline)

```
ROADMAP (фазы, ручная)
  → backlog.md (плоский список #ID задач)
    → /plan (декомпозиция одной задачи на шаги)
      → /implement (TDD по шагам плана)
```

### Где это ломается

**1. Большая фича = много задач, а не много шагов.**

Пример: "Post-Deploy Smoke Tester" (#25) — 7 шагов, один сервис, один репо. Это ок.

Контрпример: "Agent Hierarchy & Incident Response" (#2) — Watchdog, DLQ consumer, Diagnostician, Ops Executor, request_help tool, agent_configs в БД, dynamic overrides. Это 6-8 отдельных задач, каждая со своим планом, тестами, E2E. Плюс задевает shared/, api, langgraph, scheduler, worker-manager, orchestrator-cli.

**2. Cross-repo фичи не описываются одной задачей.**

"Добавить батарейку к существующему проекту" — затрагивает и service-template (copier update flow) и оркестратор (новый subgraph type). Сейчас это разные задачи в разных бэклогах без явной связи.

**3. Нет промежуточных гейтов.**

В `/plan` есть шаги, но между ними нет "запусти E2E" или "сделай аудит". Для большой фичи это критично — нужно валидировать прогресс после каждого логического блока.

**4. Нет visibility на прогресс фичи.**

STATUS.md показывает текущую задачу (#25, шаг 1/7). Но "насколько готова Phase 2: Reliability?" — нужно вручную считать done vs pending в бэклоге.

---

## Аналогии из Scrum/Agile

| Концепция | Scrum | У нас сейчас | Предложение |
|-----------|-------|-------------|-------------|
| **Epic** | Крупная фича, разбивается на stories | Нет | `docs/epics/<name>.md` |
| **Story** | Единица работы с acceptance criteria | `backlog.md #ID` | Без изменений |
| **Task** | Подзадача story | `plan.md` шаг | Без изменений |
| **Sprint** | Временной интервал | `/checkpoint` каждые 5-7 задач | Без изменений |
| **Definition of Done** | Критерии завершения | E2E pass + unit tests | Добавить на уровне эпика |
| **Sprint Review** | Оценка прогресса | `/checkpoint` | Добавить эпик-статус |

**Что берём**: Epic → Stories разбивка, DoD на уровне эпика, progress tracking.
**Что не берём**: Story Points, Sprint Planning, Velocity — overhead без пользы при 1 разработчике (агент).

---

## Options

### Option A: Эпик как документ (`docs/epics/<name>.md`)

Новый тип артефакта. Эпик = набор связанных задач из бэклога + DoD + прогресс.

```markdown
# Epic: Agent Hierarchy & Incident Response

> **Status**: in-progress (3/8 tasks done)
> **Phase**: Phase 2 — Reliability & Self-Recovery
> **DoD**: Watchdog ловит 80% инцидентов автоматически, E2E Level D pass
> **Repos**: orchestrator, (service-template — optional)

## Tasks (ordered)

1. [x] #30 DockerEventsListener в scheduler
2. [x] #31 DLQ consumer в scheduler
3. [ ] #32 Watchdog service + 5 playbooks ← current
4. [ ] #33 request_help tool в orchestrator-cli
5. [ ] #34 agent_configs миграция в БД (API)
6. [ ] #35 Diagnostician (LLM read-only)
7. [ ] #36 Ops Executor (agent, scoped write)
8. [ ] #37 Dynamic config overrides

## Gates (между логическими блоками)

- After #31: `/audit` — убедиться что DLQ интегрирован чисто
- After #33: `/e2e-run` — request_help работает end-to-end
- After #37: `/e2e-run` + `/audit` — полный incident response pipeline

## Cross-repo Dependencies

- service-template: нет
- prod_infra: ansible role для Watchdog alerts (optional, #38)

## Notes

- Brainstorm: docs/brainstorms/agent-hierarchy.md
- Можно срезать: #36 и #37 — post-MVP, если #32-#35 стабильны
```

**Жизненный цикл:**

```
/brainstorm → (большая тема) → Action Item: "epic"
  → Человек/агент создаёт docs/epics/<name>.md
  → Задачи из эпика добавляются в backlog.md с тегом `Epic: <name>`
  → /next берёт задачи из бэклога как обычно
  → /checkpoint считает прогресс по эпикам
  → Gates проверяются автоматически между задачами
```

- (+) Явная структура, progress tracking, gates
- (+) Backlog остаётся плоским — скиллы не ломаются
- (+) Эпик = "живой план" который обновляется по мере работы
- (+) Cross-repo зависимости видны в одном месте
- (-) Новый тип артефакта — ещё один файл для поддержки
- (-) Нужен скилл `/epic` для создания и обновления
- (-) Дублирование: задача описана и в эпике, и в бэклоге

### Option B: Эпик как группировка в backlog.md

Без нового артефакта. Группируем задачи прямо в бэклоге:

```markdown
## Queue

### Epic: Agent Hierarchy (#E2)
> **DoD**: Watchdog ловит 80% инцидентов автоматически
> **Progress**: 0/8
> **Gates**: after #31 → audit, after #33 → e2e

#### #30 DockerEventsListener
- **Priority**: HIGH
- **Epic**: #E2
- **Status**: pending
- **Brief**: ...

#### #31 DLQ consumer
- **Priority**: HIGH
- **Epic**: #E2
- **Status**: pending
- **Brief**: ...

### Standalone tasks

#### #25 Post-Deploy Smoke Tester
...
```

- (+) Один файл — нет рассинхрона
- (+) Порядок задач внутри эпика = порядок выполнения
- (+) Скиллы видят всё в одном месте
- (-) backlog.md становится огромным при 3-4 эпиках по 5-8 задач
- (-) Задачи из разных эпиков сложно приоритизировать друг относительно друга
- (-) Cross-repo зависимости неудобно описывать в плоском файле
- (-) Нет места для развёрнутых notes, gates, context

### Option C: Эпик = расширенный brainstorm + теги в бэклоге

Brainstorm уже создаёт Action Items. Расширить его: brainstorm с `type: epic` не просто генерирует идеи, а декомпозирует фичу на задачи.

Задачи в бэклоге получают тег `Epic: <name>`. Progress считается по тегам.

```markdown
# Brainstorm: Agent Hierarchy (epic)

> **Status**: done
> **Type**: epic
> **DoD**: ...

## Декомпозиция
(анализ, trade-offs, порядок)

## Action Items
- → new task: "#30 DockerEventsListener" (epic: agent-hierarchy)
- → new task: "#31 DLQ consumer" (epic: agent-hierarchy)
...
- → gate: after #31 → audit
- → gate: after #33 → e2e
```

- (+) Переиспользует существующий артефакт (brainstorm)
- (+) Не добавляет новый тип документа
- (-) Brainstorm = thinking, epic = tracking. Смешение ответственностей
- (-) Brainstorm после `/triage` получает status: triaged и "замораживается" — а эпик живёт долго
- (-) Gates и progress нужно считать по бэклогу, нет единого view

### Option D: Двухуровневый план

Не создавать новый артефакт. `/plan` для больших задач генерирует "мета-план" — где каждый шаг = отдельная задача в бэклоге (со своим `/plan`).

```
/plan #2 "Agent Hierarchy"
  → создаёт docs/plans/agent-hierarchy.md с мета-шагами
  → каждый мета-шаг = /triage → новая задача в бэклоге
  → /next берёт задачи как обычно
  → после каждого мета-шага — gate (e2e/audit)
```

- (+) Минимальные изменения — переиспользуем `/plan`
- (+) Вложенность: мета-план → задача → план задачи → шаги
- (-) Два уровня `/plan` — путаница: какой план сейчас актуален?
- (-) Мета-план не обновляется когда задачи завершаются
- (-) Нет явного progress tracking

---

## Сравнение

| Критерий | A (docs/epics/) | B (в backlog) | C (brainstorm+) | D (мета-план) |
|----------|-----------------|---------------|------------------|----------------|
| Новые артефакты | +1 тип | 0 | 0 | 0 |
| Новые скиллы | `/epic` | Обновить `/next`, `/checkpoint` | Обновить `/brainstorm`, `/triage` | Обновить `/plan` |
| Progress tracking | Отличный | Средний | Слабый | Слабый |
| Cross-repo | Да | Плохо | Средне | Нет |
| Gates | Явные | Средне | Средне | Средне |
| Сложность реализации | Средняя | Низкая | Низкая | Низкая |
| Масштабируемость | Хорошая | Плохая (файл растёт) | Средняя | Средняя |

---

## Перенос в оркестратор

Ключевой вопрос: как эти паттерны переносятся в сам продукт?

### Сейчас (оркестратор)

```
PO получает запрос → engineering:queue → один worker делает всё
```

Нет декомпозиции. "Сделай todo-app" = одна задача, один worker. Работает для простых проектов.

### Когда понадобится

- "Сделай todo-app с авторизацией, Redis-кешем и Telegram-ботом" — это 3+ отдельных куска работы
- "Добавь фичу к существующему проекту" — нужен анализ, план, реализация, тестирование
- Параллельные воркеры на разных модулях одного проекта

### Как паттерн эпиков ложится на оркестратор

| Dev pipeline (мы) | Оркестратор (продукт) |
|---|---|
| Человек создаёт эпик | PO/Architect создаёт "project plan" |
| Эпик → задачи в бэклоге | Project plan → задачи в engineering:queue |
| Gates между задачами | Checkpoints между фазами (CI green, smoke pass) |
| `/checkpoint` для progress | Автоматический progress report пользователю |
| Cross-repo deps | Multi-service dependencies в project spec |

**Architect node (Phase 5 roadmap)** — это по сути `/plan` для оркестратора. Декомпозирует сложный запрос на задачи для Engineering subgraph.

Разница: у нас скиллы работают с человеком в цикле. В оркестраторе нужно автоматически:
1. Определить что задача "большая" (Assessor)
2. Декомпозировать (Architect)
3. Выстроить порядок и зависимости
4. Запускать workers последовательно/параллельно
5. Гейтить (CI → smoke test → next task)
6. Репортить прогресс пользователю

---

## Option E: Внешнее хранилище задач (Task Store)

Фундаментальный сдвиг: задачи живут не в markdown-файлах, а в БД/сервисе. Markdown — view, не source of truth.

### Проблема с файлами

Markdown-бэклог работает, пока:
- 1 репо, 1 разработчик (агент), задачи линейны
- Скилл парсит файл регуляркой — хрупко, ломается при нестандартном форматировании
- Cross-repo = два файла в двух репо, синхронизация вручную
- Нет query: "покажи все задачи эпика X" = grep по файлу
- Нет истории: кто когда поменял статус — только git blame

### Варианты реализации

#### E1: GitHub Issues/Projects

Уже есть, бесплатно, API через `gh`.

```
/next → gh issue list --label "queue" --sort priority
/implement → gh issue edit #42 --add-label "in-progress"
backlog.md → auto-generated view (или не нужен)
```

- (+) Cross-repo из коробки (GitHub Projects span repos)
- (+) UI бесплатно, labels, milestones, assignees
- (+) `gh` CLI — агенты умеют
- (+) Нулевая стоимость реализации
- (-) Rate limits GitHub API (5000/час, хватит, но всё же)
- (-) Vendor lock-in на GitHub
- (-) Latency: каждый `/next` = HTTP запрос vs чтение файла
- (-) Не переносится в оркестратор напрямую — это внешний сервис

#### E2: Своя "Jira" в оркестраторе (таблицы в БД)

Расширяем существующую БД. Сейчас есть `Task` (runtime: engineering/deploy) — добавляем `WorkItem` (planning: backlog задачи) и `Epic`.

```
┌──────────────────────────────────────────────┐
│                   БД оркестратора             │
│                                              │
│  ┌─────────┐    ┌────────────┐    ┌───────┐  │
│  │  Epic    │───→│  WorkItem   │───→│ Task  │  │
│  │ (план)   │    │ (бэклог)    │    │(runtime)│ │
│  └─────────┘    └────────────┘    └───────┘  │
│                                              │
│  Epic: "Agent Hierarchy"                     │
│    → WorkItem: "Watchdog service" (pending)   │
│    → WorkItem: "DLQ consumer" (done)          │
│      → Task: engineering (completed, 14min)   │
│      → Task: deploy (completed, 3min)         │
│                                              │
└──────────────────────────────────────────────┘
```

**Модели:**

```python
class Epic(Base):
    __tablename__ = "epics"
    id: Mapped[str]           # "agent-hierarchy"
    title: Mapped[str]        # "Agent Hierarchy & Incident Response"
    status: Mapped[str]       # draft | active | done
    definition_of_done: Mapped[str]  # "Watchdog ловит 80% инцидентов"
    phase: Mapped[str | None] # ссылка на фазу roadmap
    repos: Mapped[list]       # ["orchestrator", "service-template"]

class WorkItem(Base):
    __tablename__ = "work_items"
    id: Mapped[int]           # auto-increment, #30, #31...
    epic_id: Mapped[str | None]  # FK → epics
    title: Mapped[str]
    status: Mapped[str]       # pending | in_progress | done | cancelled
    priority: Mapped[int]     # sort order
    brief: Mapped[str]        # описание
    repo: Mapped[str]         # "orchestrator" | "service-template"
    depends_on: Mapped[list]  # [29, 30] — блокеры
    gate_after: Mapped[str | None]  # "e2e" | "audit" | None

class WorkItemGate(Base):
    __tablename__ = "work_item_gates"
    work_item_id: Mapped[int] # FK → work_items (gate ПОСЛЕ этого item)
    gate_type: Mapped[str]    # "e2e" | "audit" | "manual"
    status: Mapped[str]       # pending | passed | failed
    result: Mapped[dict | None]  # ссылка на e2e report, audit findings
```

**API:**

```
GET  /work-items?epic=agent-hierarchy&status=pending
POST /work-items  (создать)
PUT  /work-items/31/status  (pending → in_progress → done)
GET  /epics/agent-hierarchy/progress  → { total: 8, done: 3, blocked: 1 }
GET  /work-items/31/gates  → [{ type: "e2e", status: "pending" }]
```

- (+) Единый source of truth для всех репо
- (+) Query, фильтрация, сортировка — SQL, не regex
- (+) История изменений (updated_at, можно добавить audit log)
- (+) **Прямой перенос в продукт** — те же таблицы для пользовательских проектов
- (+) Gates как first-class entity с результатами
- (+) Связь WorkItem → Task: видно какие runtime tasks породил этот work item
- (-) Нужно поднять сервисы для работы со скиллами (make up)
- (-) Нужен API + миграции — работа на несколько задач
- (-) Overhead для простых проектов (наш оркестратор — пока один разработчик)

#### E3: Гибрид — файлы + индекс

Markdown-файлы остаются source of truth, но скиллы работают через JSON-индекс:

```
docs/backlog.md (human-readable, редактируется руками)
  ↕ sync
.claude/backlog-index.json (machine-readable, парсится скиллами)
```

- (+) Не нужен сервер
- (+) Человек продолжает редактировать markdown
- (-) Sync — ещё один источник багов
- (-) Cross-repo не решает
- (-) Не переносится в оркестратор

### Сравнение с Options A-D

| Критерий | A (epics/) | E1 (GitHub) | E2 (своя БД) | E3 (гибрид) |
|----------|------------|-------------|---------------|-------------|
| Cross-repo | Средне | Отлично | Отлично | Плохо |
| Query/Filter | Нет | Да | Да | Частично |
| Перенос в продукт | Нет | Нет | **Да** | Нет |
| Стоимость реализации | Низкая | Низкая | Средняя | Низкая |
| Работает offline | Да | Нет | Нет (нужен make up) | Да |
| UI для человека | Markdown | GitHub UI | Нет (API only) | Markdown |

### Эволюционный путь

```
Сейчас              Скоро                  Потом (продукт)
─────────           ──────                 ─────────────
backlog.md    →   E2 (своя БД)      →   Те же таблицы для
(скиллы парсят)   (скиллы через API)     пользовательских проектов
                  backlog.md =            + Architect node
                  auto-generated view     + Assessor node
```

Ключевой инсайт: **если всё равно строить task management в оркестраторе для продукта — зачем строить его дважды?** Первый пользователь task store = мы сами (dogfooding).

### Два уровня задач (не путать!)

```
WorkItem (planning layer)          Task (execution layer)
────────────────────              ─────────────────────
"Сделать Watchdog"                "engineering run #abc123"
Создаёт: человек/Architect        Создаёт: оркестратор автоматически
Живёт: дни/недели                 Живёт: минуты/часы
Статус: pending→done              Статус: queued→running→completed
Связь: 1 WorkItem → N Tasks      Связь: 1 Task → 1 WorkItem (parent)
```

Текущая `Task` модель — это execution layer. `WorkItem` — planning layer поверх.

### Как скиллы мигрируют на Task Store

| Скилл | Сейчас (файлы) | С Task Store |
|-------|---------------|--------------|
| `/next` | Парсит backlog.md, пишет STATUS.md | `GET /work-items?status=pending&limit=1`, `PUT /work-items/31/status` |
| `/implement` | Читает STATUS.md, обновляет backlog.md | Берёт work item из API, создаёт Task, обновляет статус |
| `/triage` | Парсит e2e reports, пишет в backlog.md | Парсит reports, `POST /work-items` |
| `/checkpoint` | Считает задачи в backlog.md | `GET /epics/progress`, `GET /work-items?status=done&since=...` |
| `/epic` (новый) | — | `POST /epics`, `POST /work-items` (bulk) |

### Для продукта (оркестратор)

Те же таблицы, но `WorkItem` создаёт Architect node, а не человек:

```
User: "Сделай todo-app с авторизацией и Redis-кешем"
  → PO → Assessor: "сложная задача, нужна декомпозиция"
  → Architect node:
    POST /epics { title: "Todo App", dod: "deployed, smoke pass" }
    POST /work-items { epic: "todo-app", title: "Scaffold project" }
    POST /work-items { epic: "todo-app", title: "Auth module", depends_on: [1] }
    POST /work-items { epic: "todo-app", title: "Redis cache", depends_on: [1] }
    POST /work-items { epic: "todo-app", title: "Integration", depends_on: [2,3], gate: "e2e" }
  → Orchestrator берёт WorkItems по порядку → engineering:queue → Tasks
  → Gate после #4: smoke test
  → Progress: "Todo App: 2/4 tasks done" → Telegram
```

---

## Открытые вопросы

1. **Гранулярность**: Когда задача "достаточно большая" для эпика? Порог: >3 задач в бэклоге? >2 сервисов? >1 репо?

2. **Кто декомпозирует**: Человек (через `/brainstorm` → ручная нарезка)? Или скилл `/epic` автоматически? В оркестраторе — однозначно автоматически (Architect node).

3. **Приоритизация между эпиками**: Если два эпика в работе — какие задачи делать первыми? Interleaving vs sequential?

4. **Gates — кто дёргает**: `/next` перед взятием задачи проверяет "нет ли gate перед ней"? Или `/implement` при завершении?

5. **Отмена/срезание эпика**: "Agent Hierarchy слишком большой, давай только Watchdog + DLQ". Как отрезать хвост без ручной правки? С Task Store — `PUT /work-items/36/status cancelled`.

6. **Cross-repo координация**: Задача A в orchestrator зависит от задачи B в service-template. С Task Store — обе в одной БД, `repo` поле различает. Кто отслеживает что B done?

7. **Когда строить Task Store**: Сейчас (dogfooding) или позже (когда дойдём до Architect node)? Файлы работают, но каждый новый скилл = ещё один парсер markdown.

8. **backlog.md судьба**: Убрать полностью (source of truth = БД)? Или генерировать из БД как read-only view для человека?

9. **Миграция**: Как перенести 15+ задач из backlog.md в БД? Одноразовый скрипт? Или постепенно — новые задачи в БД, старые доживают в файле?

10. **API-first скиллы**: Если скиллы ходят в API — нужен `make up` для работы. Блокер для offline-сценариев (самолёт, нет Docker). Критично ли?

---

## Decisions

### D1: Эпики — сразу в Task Store, post-MVP

Промежуточный формат (файлы `docs/epics/`) отменён — не стоит строить то, что выкинем через фазу. Для Phase 2A эпики не нужны (линейная работа, `/plan` справляется). Эпики появятся вместе с Task Store в Phase 3.

### D2: Task Store (Phase 3, post-MVP)

**Решение: E2 — своя БД (Epic + WorkItem + WorkItemGate)**. Dogfooding: строим для себя, потом те же таблицы для продукта. Файлы остаются read-only view.

### D3: Roadmap restructure

**Решение: Split Phase 2 → 2A (pre-MVP) + 2B (post-alpha)**. Phase 2A = ~8 задач (multi-user isolation + infra + US3). После 2A — alpha release.

### D4: Pre-MVP scope

**Решение: 3 блока — multi-user (3 задачи), infrastructure (3 задачи), product (2 задачи).** Всё что не в этом списке — post-MVP.

---

---

## MVP Readiness: что блокирует альфа-пользователей

### Что работает (Phase 1 done)

Полный pipeline: описание → scaffold → code → CI → deploy → уведомление. E2E pass на todo_api (14 min) и weather_bot (15 min). Два модуля, Telegram-боты.

### Что сломается при 2-3 пользователях одновременно

Аудит multi-user изоляции выявил конкретные проблемы:

**CRITICAL — Data Leaks:**

| Проблема | Где | Суть |
|----------|-----|------|
| API auth bypass | `api/routers/projects.py:158-167` | Без `X-Telegram-ID` header → возвращает ВСЕ проекты всех пользователей. То же в tasks.py, allocations.py |
| PO не передаёт user_id | `langgraph/src/po/tools.py` (#27) | `create_project` создаёт с `owner_id=NULL`, `list_projects` возвращает всё |

**HIGH — Cross-user Actions:**

| Проблема | Где | Суть |
|----------|-----|------|
| Engineering worker: нет проверки ownership | `engineering_worker.py` | Не проверяет что `user_id` владеет `project_id` перед запуском |
| Deploy worker: то же | `deploy_worker.py:259-266` | Получает project, но не валидирует владельца |
| Port allocation race | `tools/allocator.py:54-122` | Два параллельных деплоя → оба читают порты → дупликат. Нет атомарности |

**MEDIUM — Shared State:**

| Проблема | Где | Суть |
|----------|-----|------|
| Shared uv-cache | `container_config.py:75` | Все воркеры шарят один volume → cache poisoning теоретически |
| Task update bypass | `api/routers/tasks.py:147-174` | Невалидный telegram_id проходит проверку (silent pass при user=None) |

**Что НЕ проблема** (уже изолировано):
- PO consumer: per-user locks, thread_id = `po-user-{user_id}` ✓
- Worker containers: per-project network, уникальные имена ✓
- Redis streams: namespaced by request_id ✓

### Что не отлажено (US3: добавление фичи)

`action=feature/fix` в EngineeringMessage существует, но:
- PO не умеет выбирать существующий проект (нет tool для этого)
- Нет E2E теста на feature-add
- Worker получает существующий workspace (git pull), но flow не протестирован
- Нет smoke test после feature deploy

### Предложение: Pre-MVP как отдельная фаза

Текущий roadmap:
```
Phase 1 (done) → Phase 2 (Reliability) → Phase 3 (Dev Process) → Phase 4 (MVP)
```

Проблема: Phase 2 содержит 8 задач, из которых для альфы нужны 3-4, а остальные — post-MVP. Phase 4 (MVP) включает Admin UI и Assessor — а это месяцы работы.

**Предложение — Phase 2 split:**

```
Phase 1 (done)
  ↓
Phase 2A: Pre-MVP (alpha-блокеры, 2-3 недели)
  ↓
──── ALPHA RELEASE ────
  ↓
Phase 2B: Stability (post-alpha, по фидбеку)
  ↓
Phase 3: Dev Process (Task Store, скиллы)
  ↓
Phase 4: Public Beta (Admin UI, Assessor)
```

### Phase 2A: Pre-MVP (блокеры для альфы)

Только то, без чего нельзя пустить 2-3 человек:

| # | Задача | Почему блокер | Effort |
|---|--------|--------------|--------|
| NEW | **Multi-user isolation fix** | Data leaks, cross-user actions | M |
| #27 | **PO tools: pass user_id** | Проекты без владельца, list_projects = всё всем | S |
| NEW | **Port allocation locking** | Параллельный деплой = коллизия портов | S |
| NEW | **US3: add feature flow** | Core value — "допили мне бота". Без этого одноразовый продукт | M |
| #29 | **Fix ORCHESTRATOR_USER_ID defaults** | Audit trail broken | XS |

**~5 задач, 2-3 недели.** После этого можно звать альфа-тестеров.

**Что НЕ входит** (отложено в 2B):
- #8 Workspace failure counter — annoying, не critical
- #21 Deploy pre-check — nice to have
- #10 Worker lifecycle (pause/unpause) — оптимизация
- #2 Agent hierarchy — post-MVP
- #4 CI pipeline redesign — dev experience, не user-facing
- #25 Smoke tester — уже done

### Phase 2B: Post-alpha stability (по фидбеку)

| # | Задача | Зачем |
|---|--------|-------|
| #8 | Workspace failure counter | Перестать тратить деньги на зацикленные воркеры |
| #21 | Deploy pre-check | Меньше failed deploys |
| #7 | Security: deploy cleanup | Прибираться за собой на серверах |
| NEW | Shared uv-cache isolation | Per-project cache volume |
| NEW | Фиксы по фидбеку альфа-тестеров | Неизвестный scope |

### Что с "добавить фичу" (US3)

Это самая интересная pre-MVP задача. Разбивка:

1. **PO tool: select existing project** — `list_projects(user_id=X)` + tool для выбора
2. **Engineering worker: feature flow** — git pull → branch → code → CI (без scaffold)
3. **Deploy: redeploy existing** — тот же flow что create, но без allocation
4. **E2E test: feature-add scenario** — отправить feature request через PO, проверить deploy

Это кандидат на первый "эпик" — 4 связанные задачи, cross-cutting (PO + engineering + deploy).

---

## Инфраструктура самого оркестратора: что нужно для прода

### Что уже есть (неожиданно много)

- **deploy.yml** в GitHub Actions — SSH на сервер, git pull, docker compose up, alembic migrate. Базовый, но рабочий
- **Persistent volumes** — db_data, redis_data, caddy-data, registry-data переживают `docker compose down`
- **Caddy + auto-TLS** — HTTPS из коробки, сертификаты в persistent volume
- **Health checks** — api, db, redis, worker-manager проверяются
- **Fernet encryption** — секреты проектов шифруются в БД
- **Alembic migrations** — 19 миграций, `make migrate` работает

### Что сломается при переходе на прод

**1. SSH ключи привязаны к dev-машине**

```yaml
# docker-compose.yml — сейчас
volumes:
  - ~/.ssh:/root/.ssh:ro  # SSH ключи хоста монтируются в контейнеры
```

Проблема: на прод-сервере `~/.ssh` = ключи этого сервера, а не оператора. infra-service монтирует `/host-ssh:ro` и копирует в контейнер. Нужен **dedicated SSH key** для оркестратора, а не ключи хоста.

Решение: генерировать `ORCHESTRATOR_SSH_PRIVATE_KEY` один раз, хранить как секрет (GitHub Secret или vault). При деплое записывать в `/opt/secrets/orchestrator_ssh_key`. Монтировать этот файл, а не `~/.ssh`.

**2. GitHub App PEM в репозитории**

```
secrets/github_app.pem  ← checked into git!
```

Сейчас работает потому что репо приватный. Но это плохая практика. Нужно:
- Убрать из git, добавить в `.gitignore`
- Хранить как GitHub Secret (`GH_APP_PRIVATE_KEY`)
- deploy.yml уже пишет его на сервер — осталось убрать из репо

**3. `make nuke` уничтожает всё**

```bash
# Makefile — make nuke удаляет:
docker volume rm db_data redis_data caddy-config registry-data
```

На проде `make nuke` = потеря всех данных пользователей. Нужно:
- **Убрать `make nuke` из production** (или добавить `ENVIRONMENT` guard)
- **Backup стратегия**: `pg_dump` по крону, минимум ежедневно
- **Migration-only обновления**: `docker compose up -d --build` + `alembic upgrade head`, без nuke

**4. Zero-downtime deploy не существует**

Сейчас `docker compose up -d` перезапускает все сервисы одновременно. При 2-3 активных пользователях:
- PO consumer рестартится → потеря сообщений в Redis stream? (нет — Redis persistent, consumer group подхватит)
- Engineering worker рестартится → потеря прогресса текущей задачи? (да — worker контейнер убивается)
- API рестартится → 502 на webhook от GitHub? (да — retry спасёт, GitHub ретрайит webhooks)

Для альфы с 2-3 людьми: **допустимо**. Deploy в low-traffic часы. Но нужно понимать риски.

**5. Секреты в `.env` файле**

17+ секретов в одном `.env` на сервере. Нет ротации, нет аудита, нет версионирования. Для альфы достаточно, но:
- SOPS + age уже настроен (env vars есть), но не используется
- `.env` на сервере = single point of failure (потеря файла = потеря всех секретов)

**6. Worker base images строятся локально**

```bash
make rebuild-worker-images  # Строит worker-base-common, worker-base-claude
```

На проде нужно либо пушить в registry, либо строить при деплое. deploy.yml делает `docker compose up -d` но worker base images не являются compose сервисами — они строятся отдельно.

### Что реально нужно для альфы (минимум)

| # | Задача | Effort | Блокер? |
|---|--------|--------|---------|
| 1 | **Убрать PEM из git** + gitignore + deploy.yml пишет из секрета | XS | Да (security) |
| 2 | **Dedicated SSH key** вместо host ~/.ssh | S | Да (portability) |
| 3 | **DB backup cron** — pg_dump ежедневно | XS | Да (data safety) |
| 4 | **Протестировать deploy.yml** end-to-end на прод-сервере | S | Да (без этого нет прода) |
| 5 | **Worker images в deploy pipeline** — build при деплое | S | Да (worker'ы не заработают) |
| 6 | **Environment guard** на make nuke | XS | Nice-to-have |
| 7 | **SOPS для .env** | M | Post-alpha |

### Обновлённая Phase 2A

```
Phase 2A: Pre-MVP (блокеры для альфы)

── Multi-user ──
  Multi-user isolation fix (API auth, ownership)     M
  #27 PO tools: pass user_id                         S
  Port allocation locking                             S

── Infrastructure ──
  Prod deploy pipeline (test + fix deploy.yml)        S
  Remove PEM from git + dedicated SSH key             S
  DB backup + worker images in deploy                 S

── Product ──
  US3: add feature to existing project                M
  #29 Fix ORCHESTRATOR_USER_ID defaults               XS

── Total: ~8 задач ──
```

---

## Action Items

### Pre-MVP blockers (Phase 2A)
- → new task: "Multi-user isolation fix" — API auth bypass + worker ownership check
- → new task: "Port allocation locking" — atomic allocate-or-fail
- → new task: "Prod deploy pipeline" — test deploy.yml e2e, worker images in pipeline, DB backup cron
- → new task: "Secrets hygiene" — remove PEM from git, dedicated SSH key, .gitignore
- → new task: "US3: Add feature to existing project" — PO tool + engineering feature flow + E2E
- → backlog #27: PO tools pass user_id — приоритет → HIGH, pre-MVP
- → backlog #29: Fix ORCHESTRATOR_USER_ID defaults — pre-MVP

### Post-MVP (Phase 2B+)
- → backlog #2: Agent Hierarchy — post-MVP
- → idea: Task Store в БД (Epic + WorkItem + WorkItemGate) — Phase 3, dogfooding
- → idea: Миграция скиллов на API-first — Phase 3
- → idea: Assessor node (Phase 4), Architect node (Phase 5)
- → idea: SOPS для .env на проде — Phase 2B
- → idea: Zero-downtime deploy (rolling restart) — Phase 2B

### Meta (already done)
- ~~Roadmap rewrite~~ — done in this session, ROADMAP.md updated
- ~~Triage skill: add reorder step~~ — done in this session