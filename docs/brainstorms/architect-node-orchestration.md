# Brainstorm: Architect Node — Story → Tasks Orchestration

> **Дата**: 2026-03-08
> **Контекст**: Переход от 1 story = 1 run к story → tasks → runs с автоматической декомпозицией
> **Status**: done
> **Связано**: [product-technical-split](product-technical-split.md) (done)

---

## Current State

- PO создаёт story → `create_story` сразу создаёт 1 Run → engineering worker
- Architect skill существует для dogfooding (CLI: `/architect story-xxx`)
- Task model готов: `story_id`, `blocked_by_task_id`, `acceptance_criteria`, `plan`
- Task statuses: `backlog → todo → in_dev → in_ci → testing → done` (+ blocked, failed, cancelled)
- Engineering worker получает Run из Redis queue, спавнит worker container, ждёт результат

## Problem

Сейчас 1 story = 1 engineering run с полным описанием. Проблемы:
1. **Нет декомпозиции** — большие stories дают плохой результат (LLM теряет контекст)
2. **Нет параллелизма** — независимые части выполняются последовательно
3. **Нет retry на уровне задач** — если 1 часть провалилась, перезапускается всё
4. **Нет зависимостей** — нельзя сказать "сначала DB schema, потом API, потом бот"

## Архитектура: Architect как middleware

### Flow

```
PO create_story() → Story (created)
                        ↓
              Architect Node (LangGraph)
                        ↓
              Tasks (todo, с зависимостями)
                        ↓
              Task Dispatcher (scheduler/consumer)
                        ↓
              Run per task → Engineering Worker
                        ↓
              Task done → check story completeness
```

### Q1: Architect — как триггерится?

**Вариант A: Inline в create_story (sync)**
- PO вызывает `create_story` → внутри вызывается architect → tasks создаются → runs запускаются
- (+) Простота, PO сразу получает результат
- (-) Долго — LLM call для декомпозиции + создание tasks. PO ждёт 30+ секунд
- (-) Если architect упадёт — create_story целиком упадёт

**Вариант B: Отдельный Redis stream consumer (async)**
- `create_story` создаёт Story (created) → публикует в `architect:queue`
- Architect consumer читает, декомпозирует, создаёт tasks
- (+) Async — PO не ждёт
- (-) Новый consumer, новая очередь, больше инфраструктуры
- (-) PO не знает когда tasks готовы (нужен ещё один callback)

**Вариант C: API hook (event-driven)**
- `create_story` через API → API при создании story триггерит architect
- (+) Centralized — вся логика в API
- (-) API становится слишком умным (антипаттерн — бизнес-логика в API)

**→ Рекомендация: Вариант B (async consumer).**
Architect — это LLM call, он медленный. PO не должен ждать. `create_story` создаёт story со статусом `created`, публикует в `architect:queue`. Architect consumer читает, декомпозирует, создаёт tasks, переводит story в `in_progress`. PO узнаёт через reminder.

### Q2: Контекст для декомпозиции

Architect должен понимать проект. Источники:

1. **Story description** — основной input (уже есть в story)
2. **Project config** — `project.config.detailed_spec`, modules, name
3. **Existing stories/tasks** — чтобы не дублировать и учитывать контекст
4. **Codebase (для feature/fix)** — существующая структура, API endpoints, DB schema

**Для MVP**: story description + project config + existing stories/tasks (всё через API).
**Позже**: context packer для codebase (GitHub API → tree → key files).

### Q3: Task → Run mapping

**Кто создаёт Run для task?**

**Вариант A: Architect создаёт tasks + runs сразу**
- (-) Architect не знает порядок выполнения (зависимости)
- (-) Все runs создадутся сразу, включая blocked tasks

**Вариант B: Task Dispatcher — отдельный процесс**
- Периодически (или event-driven) проверяет tasks в статусе `todo`
- Если task unblocked → создаёт Run → публикует в engineering queue → task → `in_dev`
- (+) Управляет зависимостями
- (+) Можно rate-limit (не запускать 10 runs одновременно)
- (-) Ещё один процесс

**Вариант C: Engineering worker сам берёт tasks**
- Worker читает из `engineering:queue`, но queue содержит task_id, не run описание
- Worker сам создаёт Run, берёт description из task
- (-) Worker становится слишком умным

**→ Рекомендация: Вариант B (Task Dispatcher).**
Реализация: scheduler job (уже есть scheduler service). Каждые 30с проверяет tasks в `todo` где blocker = done или null. Создаёт Run, публикует в engineering queue, переводит task в `in_dev`.

### Q4: Task lifecycle автоматизация

Кто двигает task по статусам?

```
todo ──[dispatcher creates run]──→ in_dev
in_dev ──[worker pushes code]──→ in_ci
in_ci ──[CI passes]──→ done
in_ci ──[CI fails, worker fixes]──→ in_ci (retry)
in_dev ──[worker fails]──→ failed
```

**Предложение:**
- `todo → in_dev`: Task Dispatcher (при создании Run)
- `in_dev → in_ci`: Engineering worker (при push to GitHub)
- `in_ci → done`: Engineering worker (после CI pass)
- `in_dev/in_ci → failed`: Engineering worker (при исчерпании retry)
- Engineering worker уже обновляет Run status через API. Добавить: обновлять Task status параллельно.

**Связь Task ↔ Run**: Run.run_metadata уже содержит `story_id`. Добавить `task_id` в run_metadata. Engineering worker при завершении обновляет и Run и Task.

### Q5: Story auto-complete

**Триггер**: при переходе task в `done` — API проверяет "все tasks этой story done?"

```python
# В API: POST /api/tasks/{id}/complete
# After transition:
tasks = get_tasks_by_story(task.story_id)
if all(t.status == "done" for t in tasks):
    story.status = "completed"
    # → publish story-level event to po:input
```

**Для failed**: если любой task → `failed` и нет автоматического retry — story остаётся `in_progress`. PO узнаёт через reminder, решает что делать.

### Q6: Зависимости между tasks

Task Dispatcher логика:
```python
for task in get_tasks(status="todo"):
    if task.blocked_by_task_id:
        blocker = get_task(task.blocked_by_task_id)
        if blocker.status != "done":
            continue  # skip, still blocked
    # unblocked — create run and start
    create_run_for_task(task)
    task.status = "in_dev"
```

Простой линейный подход. Граф зависимостей не нужен — `blocked_by_task_id` одиночная ссылка, architect расставляет линейные цепочки.

### Q7: Уточняющие вопросы architect → PO

**Откладываем.** Для MVP architect работает с тем что есть. Если description слишком vague — создаёт меньше tasks с более широким scope. PO может дополнить story description и перезапустить decomposition.

### Q8: Минимальный scope

**Шаг 1: Architect consumer** (новый)
- Читает из `architect:queue`
- Получает story, project config, existing tasks
- LLM декомпозирует → создаёт tasks через API
- Story → `in_progress`

**Шаг 2: Изменить create_story** (PO tool)
- Вместо создания Run напрямую → публикует в `architect:queue`
- Убрать создание Run из create_story

**Шаг 3: Task Dispatcher** (scheduler job)
- Каждые 30с: найти tasks в `todo` без blocker (или blocker done)
- Создать Run, опубликовать в engineering queue, task → `in_dev`

**Шаг 4: Engineering worker обновляет task status**
- При push: task → `in_ci`
- При CI pass: task → `done`
- При fail: task → `failed`

**Шаг 5: Story auto-complete**
- API hook: при task → `done`, проверить все tasks story

---

## Action Items

### Реализация (ordered)
- → new task: "Architect consumer — read architect:queue, decompose story into tasks via LLM"
- → new task: "Modify create_story to publish to architect:queue instead of creating Run"
- → new task: "Task Dispatcher scheduler job — todo tasks → runs → engineering queue"
- → new task: "Engineering worker: update task status (in_dev → in_ci → done/failed)"
- → new task: "Story auto-complete API hook — all tasks done → story completed"
- → new task: "Story-level events to PO — notify on story completed/failed"

### Откложено
- → idea: "Context packer for architect — project codebase summary via GitHub API"
- → idea: "Architect ↔ PO channel — ask clarifying questions through PO"
- → idea: "Parallel task execution limits — rate-limit concurrent runs"
