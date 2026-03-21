---
id: bs-f781efac
status: done
title: "Milestone model + ROADMAP generation"
created_at: 2026-03-07T14:44:02.172067Z
---

# Brainstorm: Milestone model + ROADMAP generation

> **Дата**: 2026-03-07
> **Контекст**: ROADMAP.md устарел, Phase 3 описывает уже сделанное. Нужна модель в БД для генерации ROADMAP из API — переносимая на пользовательские проекты.
> **Status**: done

---

## Решение

Одна сущность `Milestone` покрывает и наши phases, и пользовательские epics/features.

### Почему не Epic + Milestone отдельно

- Для наших проектов (50 задач) хватит одного уровня группировки
- Для пользовательских проектов (бот с 5 фичами) — тоже
- Если потом нужна вложенность — `parent_id` FK на себя

### Модель

```python
class Milestone(Base):
    __tablename__ = "milestones"

    id: str                    # "ms-xxxx"
    project_id: str            # FK → projects
    title: str                 # "Phase 2B: Post-alpha stability"
    description: str | None    # Цели фазы
    sort_order: int            # Порядок в roadmap (0 = top)
    status: str                # open | completed
    parent_id: str | None      # FK → milestones (вложенность, Phase 2)
    created_by: str            # "user" | "system" | "po"
```

### Связь с WorkItem

`WorkItem.milestone_id: FK → milestones` (nullable). Задачи без milestone — в секции "Unsorted".

### Статус milestone

Вычисляется, не хранится? Или хранится с ручным override?
- Вариант А: computed — все work items done → completed
- Вариант Б: хранится, action endpoint `/complete` — явное закрытие
- **Рекомендация**: хранится (как WorkItem), но с автоматической подсказкой. PO/triage может закрыть milestone когда считает нужным, даже если не все задачи done.

### API

- CRUD: `POST/GET/PATCH/DELETE /api/milestones/`
- Фильтры: `?project_id=X&status=open`
- `GET /api/milestones/{id}/work-items` — work items внутри milestone
- Action: `POST /api/milestones/{id}/complete`

### Генерация ROADMAP.md

`make roadmap` / `scripts/generate_roadmap.py`:
1. `GET /api/milestones/?project_id=codegen-orchestrator` (sorted by sort_order)
2. Для каждого milestone: `GET /api/milestones/{id}/work-items`
3. Генерируем markdown: `[x]` если work item done, `[ ]` если нет
4. Completed milestones — показываем свёрнуто (только заголовок + "COMPLETE")
5. Задачи без milestone — секция "Backlog" внизу

### Для пользовательских проектов

- ПО создаёт milestones через `create_milestone` tool
- При создании проекта: шаблонные milestones (scaffold → develop → deploy)
- Юзер видит прогресс по фазам через ПО (`list_milestones`)
- Та же модель, тот же API, тот же UX

### Миграция текущего ROADMAP

Одноразовый скрипт или ручной curl:
- Phase 1 → milestone (completed)
- Phase 2A → milestone (completed)
- Phase 2B → milestone (open), привязать work items #21, #7, #10
- Phase 3 → обновить описание (большая часть сделана), milestone (open)
- Phase 4-6 → milestones (open), привязать что есть

### Связь с epic-decomposition brainstorm

Epic Decomposition рассматривал 4 варианта (A-D). Milestone = упрощённый вариант A, но в БД вместо markdown. Вложенность через `parent_id` позволяет потом добавить sub-milestones если нужно (аналог epic → sub-epic).

---

## Action Items

- → new task: "#63 Milestone model + ROADMAP generation" — модель, API, миграция, скрипт генерации, миграция текущего ROADMAP
