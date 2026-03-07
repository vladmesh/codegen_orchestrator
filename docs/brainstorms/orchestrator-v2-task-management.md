# Brainstorm: Orchestrator v2 — Task Management System

> **Дата**: 2026-03-07
> **Контекст**: Переход от "проект = один процесс" к полноценной системе управления задачами (Jira-like) с agile-статусами, историей итераций, и абстракцией ПО от кода.
> **Status**: in_progress (Step 0 complete)
> **Связано**: [epic-decomposition.md](epic-decomposition.md) (Phase 3 Task Store), [task-description-flow.md](task-description-flow.md)

---

## Current State

### Что есть сейчас

```
Project (БД)
  ├── config.description        — "Telegram bot for currency rates"
  ├── config.detailed_spec      — полная спека (только для create)
  ├── config.modules            — ["backend", "tg_bot"]
  ├── config.secrets            — зашифрованные ключи
  ├── config.env_hints          — подсказки для воркера
  ├── status                    — draft|scaffolding|developing|active|failed
  └── repository_url            — GitHub repo

Task (БД) — runtime execution
  ├── type                      — "engineering" | "deploy"
  ├── status                    — queued|running|completed|failed
  ├── task_metadata             — {action, triggered_by, ci_attempts}
  ├── result                    — {commit_sha, engineering_status}
  └── error_message / traceback
```

### Что сломано

**1. Проект = один большой процесс, нет "доработок" как сущностей.**

Сейчас "добавить фичу" = вызвать `trigger_engineering(action="feature", description="...")`. Description передаётся через Redis queue и нигде не персистится. Когда задача завершилась — описание фичи потеряно. Нельзя узнать:
- Какие фичи были добавлены к проекту
- Сколько раз фича возвращалась из CI-фикса в разработку
- Что конкретно делал воркер в каждой итерации

**2. Статус проекта — монолитный, не отражает жизненный цикл доработок.**

`Project.status = developing` — это статус ПРОЕКТА, а не конкретной фичи. Если параллельно идёт фича и багфикс — status один на всех. Нет:
- Backlog фич проекта
- Отдельных статусов per-feature (todo → in_dev → testing → done)
- Истории переходов между статусами

**3. ПО знает про сабграфы и воркеры.**

PO вызывает `trigger_engineering` напрямую — знает про engineering:queue, action types, skip_deploy. В идеале ПО должен мыслить на уровне "создать задачу для проекта" и "посмотреть статус задачи", а не "отправить сообщение в Redis queue".

**4. Нет истории итераций.**

Когда CI фейлится и воркер фиксит — это записывается в `task_metadata.ci_attempts`. Но:
- Не видно что именно воркер менял в каждой итерации
- Если задача зафейлилась и её перезапустили — это новый Task, связи со старым нет
- Нет журнала "фича X: попытка 1 — CI fail, попытка 2 — тесты упали, попытка 3 — done"

**5. Логика управления живёт в claude skills, не в оркестраторе.**

`/brainstorm`, `/triage`, `/plan`, `/audit`, `/checkpoint` — это навыки Claude Code, которые парсят markdown-файлы. Для нашей разработки это ок, но эта логика не доступна продукту (пользовательским проектам). Со временем хотим перенести эти паттерны в оркестратор.

---

## Target Vision (North Star)

```
┌─────────────────────────────────────────────────────────────┐
│                    Planning Layer                            │
│                                                             │
│  Project                                                    │
│    └── Epic (опционально, для больших фич)                  │
│          └── WorkItem (единица работы, agile-статусы)       │
│                ├── description, acceptance_criteria          │
│                ├── status: backlog → todo → in_dev →         │
│                │          in_review → testing → done         │
│                ├── history: [{status, timestamp, reason}]    │
│                └── iterations: [{attempt, worker_log, ...}] │
│                                                             │
│  PO видит ТОЛЬКО этот слой. Не знает про воркеры,           │
│  очереди, CI, сабграфы.                                     │
│                                                             │
├─────────────────────────────────────────────────────────────┤
│                   Orchestration Layer                        │
│                                                             │
│  Берёт WorkItem со статусом todo → создаёт runtime Task    │
│  → управляет жизненным циклом:                              │
│    spawn worker → CI gate → test → deploy                   │
│  → обновляет статус WorkItem по результату                  │
│  → при failure: возвращает WorkItem в backlog с причиной    │
│                                                             │
├─────────────────────────────────────────────────────────────┤
│                    Execution Layer                           │
│                                                             │
│  Task (runtime, как сейчас)                                 │
│    engineering run, deploy run, test run                     │
│    Живёт минуты/часы. Связан с WorkItem (parent).           │
│                                                             │
└─────────────────────────────────────────────────────────────┘
```

### Как ПО работает в target state

```
Пользователь: "Добавь кнопку статистики в бота"
    ↓
ПО (через tools):
    1. list_work_items(project_id) → видит текущий бэклог
    2. create_work_item(project_id, title="Кнопка статистики",
                        description="...", type="feature")
       → WorkItem создан со статусом backlog
    3. start_work_item(work_item_id)
       → статус: backlog → todo → in_dev
       → оркестратор АВТОМАТИЧЕСКИ создаёт Task, спавнит воркера
    4. set_reminder(15, "check work item wi-123")
    ↓
(15 минут)
    ↓
ПО:
    5. get_work_item(wi-123) → status: testing, iteration: 2
       → "Первая попытка: CI fail (lint errors). Вторая: тесты проходят, деплой..."
    6. Отвечает юзеру: "Фича в процессе, вторая итерация, CI проходит"
    ↓
(ещё 5 минут)
    ↓
System event: work_item wi-123 completed
    ↓
ПО: "Готово! Кнопка статистики добавлена и задеплоена."
```

**Ключевое отличие**: ПО не вызывает `trigger_engineering`. Он создаёт WorkItem и говорит "начни работу". Оркестратор сам решает как именно это сделать.

---

## Data Model

### WorkItem (новая сущность)

```python
class WorkItemStatus(str, Enum):
    BACKLOG = "backlog"       # В бэклоге, не приоритизирован
    TODO = "todo"             # Приоритизирован, готов к работе
    IN_DEV = "in_dev"         # Воркер работает
    IN_REVIEW = "in_review"   # CI/код-ревью (будущее)
    TESTING = "testing"       # Тесты/smoke tests
    DONE = "done"             # Завершён и задеплоен
    FAILED = "failed"         # Провалился окончательно (retries exhausted)
    CANCELLED = "cancelled"   # Отменён

class WorkItemType(str, Enum):
    CREATE = "create"         # Создание проекта
    FEATURE = "feature"       # Новая фича
    FIX = "fix"               # Багфикс
    REFACTOR = "refactor"     # Рефакторинг (будущее)

class WorkItem(Base):
    __tablename__ = "work_items"

    id: Mapped[str]                        # "wi-a1b2c3d4"
    project_id: Mapped[str]                # FK → projects
    epic_id: Mapped[str | None]            # FK → epics (Phase 2)
    type: Mapped[str]                      # WorkItemType
    title: Mapped[str]                     # "Кнопка статистики"
    description: Mapped[str]               # Полное описание (от ПО)
    status: Mapped[str]                    # WorkItemStatus
    priority: Mapped[int]                  # Порядок в бэклоге (0 = top)
    acceptance_criteria: Mapped[str | None]  # Когда считать done
    current_iteration: Mapped[int]         # Номер текущей попытки (0-based)
    max_iterations: Mapped[int]            # Лимит попыток (default 3)
    created_by: Mapped[str]                # "po" | "user" | "system"
```

### WorkItemEvent (история переходов)

```python
class WorkItemEvent(Base):
    __tablename__ = "work_item_events"

    id: Mapped[int]                        # auto-increment
    work_item_id: Mapped[str]              # FK → work_items
    event_type: Mapped[str]                # "status_change" | "iteration_start" |
                                           # "iteration_end" | "note"
    from_status: Mapped[str | None]        # Для status_change
    to_status: Mapped[str | None]          # Для status_change
    iteration: Mapped[int | None]          # Номер итерации
    details: Mapped[dict]                  # Произвольные данные:
                                           #   - commit_sha, ci_result, error_message
                                           #   - worker_id, duration_seconds
                                           #   - failure_reason, fix_description
    actor: Mapped[str]                     # "system" | "po" | "user"
```

### Связь WorkItem → Task (execution)

```python
# Существующая модель Task получает новое поле:
class Task(Base):
    # ... existing fields ...
    work_item_id: Mapped[str | None]       # FK → work_items (nullable для обратной совместимости)
    iteration: Mapped[int | None]          # Какая итерация WorkItem породила этот Task
```

### Пример жизненного цикла

```
WorkItem "wi-abc" (feature: "Кнопка статистики")
│
├── Event: status_change backlog → todo (actor: po)
├── Event: status_change todo → in_dev (actor: system)
│
├── Event: iteration_start #0
│   ├── Task eng-111 created (work_item_id=wi-abc, iteration=0)
│   ├── Task eng-111 completed (commit_sha=aaa111)
│   ├── Event: note "CI failed: lint errors in stats.py"
│   ├── Task eng-222 created (CI fix, iteration=0)
│   ├── Task eng-222 completed (commit_sha=aaa222)
│   └── Event: iteration_end #0 result=ci_passed
│
├── Event: status_change in_dev → testing (actor: system)
├── Event: status_change testing → in_dev (actor: system, reason: "smoke test failed")
│
├── Event: iteration_start #1
│   ├── Task eng-333 created (work_item_id=wi-abc, iteration=1)
│   ├── Task eng-333 completed (commit_sha=bbb111)
│   └── Event: iteration_end #1 result=ci_passed
│
├── Event: status_change in_dev → testing (actor: system)
├── Event: status_change testing → done (actor: system)
│
├── Event: status_change done → in_dev (actor: po, reason: "баг вернулся через 2 недели")
│   ... новый цикл итераций ...
```

---

## PO Abstraction

### Текущие PO tools → новые PO tools

| Сейчас | Target | Что меняется |
|--------|--------|-------------|
| `create_project(name, modules, description)` | **Без изменений** | Остаётся как есть |
| `trigger_engineering(project_id, action, description)` | `create_work_item(project_id, type, title, description)` + `start_work_item(work_item_id)` | ПО не знает про engineering queue |
| `get_task_status(task_id)` | `get_work_item(work_item_id)` | ПО видит статус фичи, не runtime task |
| — | `list_work_items(project_id, status?)` | Бэклог проекта |
| — | `update_work_item(work_item_id, ...)` | Изменить описание, приоритет |
| — | `reopen_work_item(work_item_id, reason)` | Вернуть из done в backlog |
| `trigger_deploy(project_id)` | Убрать (или оставить как "передеплой без изменений") | Деплой — часть жизненного цикла WorkItem |
| `set_reminder(...)` | **Без изменений** | |
| `notify_user(...)` | **Без изменений** | |
| `set_project_secret(...)` | **Без изменений** | |

### Что ПО видит при `get_work_item`

```json
{
  "id": "wi-abc",
  "title": "Кнопка статистики",
  "type": "feature",
  "status": "in_dev",
  "current_iteration": 1,
  "last_event": "CI fix attempt, lint errors resolved",
  "history_summary": "Iteration 0: CI failed (lint). Iteration 1: in progress...",
  "created_at": "2026-03-07T10:00:00Z",
  "elapsed_minutes": 12
}
```

ПО НЕ видит: worker_id, container names, git branches, redis streams, queue names.

---

## Orchestration Layer (кто управляет жизненным циклом)

Сейчас `engineering_worker.py` — это монолитная функция на 1000+ строк, которая:
1. Создаёт репо
2. Спавнит воркера
3. Ждёт CI
4. Фиксит CI failures
5. Триггерит деплой

В target state эта логика остаётся, но обёрнута в "WorkItem lifecycle manager":

```python
# Псевдокод — не финальная реализация
async def process_work_item(work_item_id: str):
    wi = await api.get_work_item(work_item_id)

    # Обновить статус
    await api.update_work_item_status(wi.id, "in_dev")
    await api.add_work_item_event(wi.id, "iteration_start", iteration=wi.current_iteration)

    # Создать Task (execution layer)
    task = await create_engineering_task(wi)  # ← текущая логика

    # Записать результат
    if task.status == "completed":
        await api.add_work_item_event(wi.id, "iteration_end",
            details={"result": "success", "commit_sha": task.result["commit_sha"]})

        # CI gate + deploy (текущая логика _wait_for_ci_and_fix)
        ci_ok = await wait_for_ci(...)
        if ci_ok:
            await deploy(...)
            await api.update_work_item_status(wi.id, "done")
        else:
            # Retry?
            if wi.current_iteration < wi.max_iterations:
                await api.increment_iteration(wi.id)
                await process_work_item(wi.id)  # рекурсия или re-queue
            else:
                await api.update_work_item_status(wi.id, "failed")
    else:
        await api.add_work_item_event(wi.id, "iteration_end",
            details={"result": "failed", "error": task.error_message})
        await api.update_work_item_status(wi.id, "failed")
```

### Ключевой принцип: engineering_worker не ломается

Текущий `engineering_worker.py` продолжает работать. WorkItem lifecycle — это обёртка ПОВЕРХ него, не замена. На переходном этапе:
- Если WorkItem есть → engineering_worker записывает события в WorkItem
- Если WorkItem нет (старый вызов через `trigger_engineering`) → работает как раньше

---

## Стратегия: Dogfooding First

Строим систему для управления разработкой самого оркестратора. Claude skills (`/next`, `/plan`, `/implement`, `/triage`) постепенно мигрируют с markdown-файлов на API. Когда модель отлажена на десятках реальных задач — переносим на продукт (PO tools).

**Почему этот порядок:**
- Мгновенная обратная связь — каждый `/next` тестирует API
- Модель закаляется на сложном кейсе (50+ задач, эпики, cross-repo)
- Не ломаем продукт экспериментами — пользователи не страдают
- Если модель для нас работает — для "бот с 3 фичами" точно хватит

**Главный риск:** зависимость от `make up` для работы со скиллами.
**Митигация:** dual-mode в скиллах (API если доступен, fallback на файлы).

---

## Migration Path (маленькие шаги)

### Шаг 0: WorkItem модель + API + миграция backlog.md (фундамент)

**Что делаем:**
- Alembic миграция: таблицы `work_items`, `work_item_events`
- Добавляем `work_item_id` + `iteration` в существующую `Task`
- API: CRUD для work_items, events (action-based status transitions)
- Скрипт миграции: парсим `docs/backlog.md` → INSERT в work_items
- `backlog.md` становится auto-generated view (read-only, генерится из БД)

**Что улучшается:**
- Бэклог в БД — SQL-запросы вместо regex по markdown
- backlog.md по-прежнему читаем для человека, но source of truth = БД
- Фундамент для всех следующих шагов

**Риск:** Низкий. Новые таблицы, ничего не ломается. Скиллы пока продолжают читать файлы.

### Шаг 1: `/next` через API

**Что делаем:**
- `/next` вместо парсинга backlog.md вызывает `GET /api/work-items?status=todo&limit=1`
- При взятии задачи: `POST /api/work-items/{id}/start` (status: todo → in_progress)
- STATUS.md по-прежнему обновляется (для контекста агента)
- Dual-mode: если API недоступен → fallback на backlog.md

**Что улучшается:**
- Первый скилл на API — proof of concept
- Статус задачи в БД, а не в markdown
- STATUS.md синхронизирован с реальным состоянием

**Обратная совместимость:**
- Fallback на файлы если `make up` не запущен
- Остальные скиллы пока работают по-старому

### Шаг 2: `/implement` пишет events

**Что делаем:**
- При старте каждого шага плана: `POST /api/work-items/{id}/events` (step_start)
- При завершении шага: event (step_done, commit_sha, test_results)
- При завершении задачи: `POST /api/work-items/{id}/complete`
- История шагов/итераций в БД, не только в git log

**Что улучшается:**
- Видно прогресс по задаче в реальном времени (через API)
- Если задача зафейлилась и вернулась — видно что было в предыдущих итерациях
- `/checkpoint` может считать прогресс по SQL, а не парсить markdown

### Шаг 3: `/triage` и `/checkpoint` через API

**Что делаем:**
- `/triage`: вместо записи в backlog.md → `POST /api/work-items` (создаёт новые задачи)
- `/checkpoint`: `GET /api/work-items?status=done&since=...` для подсчёта прогресса
- backlog.md генерируется из БД при каждом изменении (или по запросу)

**Что улучшается:**
- Все скиллы на API — markdown полностью read-only view
- Triage создаёт задачи с правильными связями (epic, priority)
- Checkpoint даёт точную статистику

### Шаг 4: Перенос на продукт (PO tools)

**Что делаем:**
- К этому моменту API стабилен и проверен на десятках задач
- Новые PO tools: `create_work_item`, `list_work_items`, `get_work_item`, `start_work_item`
- `start_work_item` внутри вызывает `trigger_engineering` (старый механизм)
- Engineering worker при наличии `work_item_id` → пишет events
- PO промпт обновляется: мыслить фичами, не engineering tasks

**Что улучшается:**
- Пользователь видит бэклог фич своего проекта
- Описание фичи персистится — больше не теряется в Redis queue
- ПО абстрагирован от execution layer
- Модель уже отлажена — PO получает battle-tested API

**Обратная совместимость:**
- `trigger_engineering` остаётся для action=create
- Старые tools доступны на переходный период

### Шаг 5: Work item lifecycle в engineering worker

**Что делаем:**
- engineering_worker при наличии work_item_id:
  - Пишет iteration_start / iteration_end events
  - CI fix attempts → events с деталями
  - Обновляет work_item.status по результату (in_dev → testing → done)
- Deploy worker → обновляет status при успешном деплое

**Что улучшается:**
- Полный audit trail: сколько итераций, что фейлилось, почему
- ПО через `get_work_item` видит детальный прогресс без знания о воркерах
- Reopen: вернуть из done → in_dev с причиной

### Шаг 6+ (будущее)

- Epic модель — группировка WorkItems для больших фич
- Architect node — автоматическая декомпозиция сложных запросов на WorkItems
- Assessor — определение сложности (простой → 1 WorkItem, сложный → Epic)
- Связи между WorkItems (related_to, blocked_by)
- Cross-repo tracking (orchestrator + service-template в одной БД)

---

## Открытые вопросы

### Q1: Как скиллы общаются с API?

Скиллы — это промпты для Claude Code. Claude Code имеет `Bash` tool. Варианты:
- **A)** `curl` из bash в скиллах — просто, но verbose
- **B)** CLI-обёртка (`python -m orchestrator_cli work-items list`) — чище, но новый пакет
- **C)** Прямой import shared.models + SQLAlchemy — без API, но связывает с БД напрямую

**Склоняюсь к A** на старте (curl), потом B когда паттерн устоится.

### Q2: Dual-mode (API + файлы) — насколько серьёзно?

Если `make up` не запущен, скиллы должны работать? Варианты:
- **A)** Обязательный `make up` — скиллы без API не работают
- **B)** Graceful fallback на backlog.md если API недоступен
- **C)** Отдельный lightweight mode (SQLite файл вместо Postgres)

**Склоняюсь к A** — мы всегда работаем с `make up`. Offline-разработка — исключение, не правило. Упрощает код скиллов (нет двух путей).

### Q3: Гранулярность events

Что записывать?
- Минимум: status changes + final result
- Средне: + iteration start/end, CI results, errors
- Максимум: + каждый шаг плана, каждый коммит, каждый CI poll

**Склоняюсь к среднему**: status changes, iteration boundaries, CI results, step completions, errors. Расширять по мере необходимости.

### Q4: Как create_project ложится в эту модель?

Создание проекта — это тоже WorkItem? Или это отдельный flow?
- **A)** WorkItem type=create создаётся автоматически при create_project
- **B)** Только feature/fix — WorkItems, create остаётся как есть

**Склоняюсь к B** на шаге 4, потом A.

### Q5: API design — REST или action-based?

- REST: `PATCH /work-items/wi-123 {"status": "in_dev"}`
- Action-based: `POST /work-items/wi-123/start`, `/complete`

**Склоняюсь к action-based** — state machine с валидными переходами.

### Q6: Миграция данных из backlog.md

50+ задач в markdown. Как переносить?
- **A)** Одноразовый скрипт-парсер
- **B)** Вручную через API (10 минут на `curl` loop)
- **C)** Перенести только Queue (активные задачи), Done/Ideas оставить в файле

**Склоняюсь к C** — перенести только актуальные задачи. Историю Done оставить в git.

### Q7: backlog.md после миграции

- **A)** Генерируется из БД автоматически (read-only view)
- **B)** Удаляется, вся работа через API
- **C)** Остаётся как human-readable snapshot, обновляется вручную при checkpoint

**Склоняюсь к A** — генерировать при каждом изменении (или команда `make backlog`).

---

## Recommendation

**Dogfooding first. Шаги 0→1→2→3 — для нашей разработки. Шаги 4→5 — перенос на продукт.**

Шаг 0 (модели + API + миграция) — фундамент + первые данные в БД. Можно остановиться и оценить.

Шаг 1 (`/next` через API) — proof of concept: один скилл работает с БД. Если неудобно — откатить тривиально.

Шаг 2 (`/implement` пишет events) — главная ценность: история итераций в БД, прогресс по задачам.

Шаг 3 (`/triage` + `/checkpoint`) — все скиллы на API, backlog.md = auto-generated view.

К шагу 4 модель отлажена на реальной работе. PO получает battle-tested API.

Каждый шаг — отдельная задача, отдельный план, отдельный PR.

---

## Action Items

### Dogfooding (наша разработка)
- → new task: "WorkItem model + API + backlog migration (Step 0)" — Alembic migration, CRUD API, action-based status transitions, скрипт миграции backlog.md → БД
- → new task: "/next skill via API (Step 1)" — переписать /next на GET/POST work-items API. Dual-mode или API-only (решить Q2).
- → new task: "/implement work item events (Step 2)" — записывать step_start/step_done/complete events. История итераций в БД.
- → new task: "/triage + /checkpoint via API (Step 3)" — triage создаёт work items через API. Checkpoint считает прогресс по SQL. backlog.md auto-generated.

### Перенос на продукт
- → new task: "PO work item tools (Step 4)" — create/list/get/start_work_item tools для ПО. start_work_item вызывает trigger_engineering внутри.
- → new task: "Engineering worker work_item lifecycle (Step 5)" — iteration events, status updates, audit trail в engineering/deploy workers.

### Будущее
- → idea: "Epic model" — группировка WorkItems, когда понадобится декомпозиция больших фич
- → idea: "Architect node" — автоматическая декомпозиция сложных запросов на WorkItems
- → idea: "Reopen + bug tracking" — reopen_work_item, related_to links
- → idea: "Cross-repo tracking" — orchestrator + service-template work items в одной БД
