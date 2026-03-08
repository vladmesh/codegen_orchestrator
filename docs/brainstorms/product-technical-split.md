# Brainstorm: Product-Technical Split — Architect Flow

> **Дата**: 2026-03-08
> **Контекст**: Переход к процессу, где PO заводит только user stories, а декомпозиция и execution автоматизированы через /architect + /plan + /implement.
> **Status**: done
> **Связано**: [orchestrator-v2-task-management](bs-d5dadb7d) (Steps 0-2 done), [epic-decomposition](bs-42566a84), [multirepo-pipeline](bs-44524c33)

---

## Что было сделано (в этой сессии)

### PO Tools — story-first workflow

**Убрано:**
- `trigger_engineering` — PO больше не знает про engineering queue
- `trigger_deploy` — PO не знает про деплои (деплой автоматический)
- `get_task_status` → переименован в `get_run_status`, читает из `/api/runs/`

**Добавлено:**
- `create_story(project_id, title, description, story_type)` — создаёт story + сразу запускает engineering run
- `list_stories(project_id)` — все stories проекта
- `get_story(story_id)` — story + привязанные tasks

**Исправлено:**
- `trigger_deploy` и старый `trigger_engineering` POSTили в `/api/tasks/` с полями Run-схемы (`id`, `task_metadata`, `callback_stream`). Это было сломано после rename WorkItem→Task. Теперь `create_story` и `trigger_deploy` (до удаления) корректно используют `/api/runs/`.
- `action` (create/feature/fix) теперь определяется **по статусу проекта**, не по `story_type`. Проект `draft` → `action=create` (scaffold), проект `active` → `action=feature` (доработка), `story_type=fix` → `action=fix`. PO не думает об этом.
- Убран мёртвый `ProjectStatus.DISCOVERED` из enum (для проектов не использовался нигде).

### PO System Prompt

Полностью переписан:
- Story-Based Workflow section — PO мыслит stories
- Убраны все упоминания `trigger_engineering`, `trigger_deploy`
- Три сценария: Create New Project, Add Features/Fix Bugs, Ask About Status
- Automatic Deploy Pipeline — PO ничего не делает, деплой автоматический

### Принципы (зафиксированные решения)

1. **PO оперирует только stories.** Никаких технических инструментов (deploy, engineering queue).
2. **Action определяется системой, не PO.** `draft` → create, иначе → feature. PO не знает разницы.
3. **"Остановить работу" = изменить статус story**, не останавливать runs напрямую. Оркестратор на своей стороне реагирует на изменение статуса story.
4. **"Остановить проект" (выключить бота/сайт)** — отдельный PO tool (будущее). Это operational action на уровне проекта, не story.
5. **Redeploy не существует** с точки зрения продукта. "Не работает" = баг → story с `story_type="fix"`. Техническая команда решает нужен ли редеплой.

---

## Оставшиеся вопросы и план

### Q1: Architect — skill или LangGraph node?

**→ Решение: гибрид.** `/architect` skill уже написан для dogfooding. Когда product нужен — портируем в LangGraph node. Оба делают одно и то же (story → tasks), просто из разных runtimes.

### Q2: Когда вызывается Architect?

**Dogfooding**: вручную (`/architect story-xxx`).
**Production (будущее)**: автоматически после `create_story`. Промежуточный шаг: `POST /api/stories/{id}/decompose`.

Сейчас `create_story` сразу создаёт один engineering run (1 story = 1 run). Architect добавится как middleware между story creation и run creation.

### Q3: Story lifecycle — cancel/stop

PO должен уметь:
- **cancel_story(story_id)** — "передумал, не надо эту фичу". Story → cancelled, оркестратор останавливает/игнорирует runs.
- **stop_project(project_id)** — "выключи бота". Operational action: останавливает deployed сервисы. Это НЕ story, это действие над проектом.

Оба — будущие PO tools. Cancel story требует:
1. Story status transition: `in_progress → cancelled`
2. Оркестратор: если есть running run для cancelled story — помечает его для остановки

### Q4: Обратная связь — story auto-completion

Когда все runs/tasks story завершены → story auto-completes. Реализация: hook в API при обновлении run status.

### Q5: Granularity и зависимости

Решено в `/architect` SKILL.md:
- 3-8 tasks per story, если больше — split story
- `Task.blocked_by_task_id` для зависимостей
- Architect расставляет линейные и параллельные зависимости

---

## Action Items

### Сделано ✓
- ~~PO story tools — create_story, list_stories, get_story~~ ✓
- ~~Update PO system prompt — story-first thinking~~ ✓
- ~~Fix PO tools to use /api/runs/ instead of /api/tasks/~~ ✓
- ~~Remove trigger_deploy from PO tools~~ ✓
- ~~Remove ProjectStatus.DISCOVERED~~ ✓
- ~~Action detection by project status (draft→create, active→feature)~~ ✓

### Следующие шаги
- → new task: "Auto-complete story when all runs done" (API hook)
- → new task: "cancel_story PO tool + story cancelled status" (story lifecycle)
- → new task: "stop_project PO tool — остановка deployed сервисов" (operational)
- → idea: "LangGraph architect node (port from /architect skill)" — когда product нужен
- → idea: "Context packer for architect — project summary for LLM"
- → backlog #59: PO work item tools — **поглощён** этой работой (закрыть/переформулировать)
- → backlog #60: Engineering worker work_item lifecycle — остаётся
