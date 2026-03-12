# Brainstorm: Story/Task Reopen + Agent Role Boundaries

> **Дата**: 2026-03-12
> **Контекст**: E2E показал что пайплайн one-way — девелопер не может эскалировать блокеры, ПО не может переоткрыть стори, контекст прошлых попыток теряется. Нужно определить роли агентов и добавить обратные потоки.
> **Status**: draft

---

## Current State

### Поток данных (однонаправленный)

```
User → PO → create_story() → architect:queue → Architect → create_task() → engineering:queue → Developer → commit
```

Обратной связи нет. Developer не может сказать "задача нерешаема". Architect не может пересмотреть декомпозицию. PO всегда создаёт новую стори вместо переоткрытия существующей.

### Что уже есть в коде

- **Story reopen**: `COMPLETED → IN_PROGRESS` — валидный переход в `VALID_TRANSITIONS`, но нигде не используется автоматически
- **Task reopen**: `DONE → BACKLOG` — есть endpoint `POST /api/tasks/{id}/reopen`, но вызывается только вручную через triage
- **Story context**: engineering consumer передаёт `story_context` (предыдущие таски + их события) — но только для тасков внутри одной стори
- **Worker reuse**: один контейнер на всю стори через `story:workers` registry

### Что не работает

1. PO `create_story()` — всегда `POST /api/stories/`, никогда не проверяет existing stories
2. Developer — единственный output это git commit. Нет канала для "я застрял" / "URLs битые" / "72% работы не получилось"
3. Architect — не получает фидбек о результатах выполнения тасков. Не может пересмотреть декомпозицию
4. При повторном баг-репорте от юзера — контекст предыдущей попытки полностью теряется

---

## Определение ролей агентов

### PO (Product Owner)

**Философия**: Чисто продуктовый подход. Не указывает КАК делать, описывает ЧТО хочет юзер.

**Границы**:
- (+) Понимает что хочет юзер, формулирует требования
- (+) Минимальная техническая экспертиза: понимает какие env vars спросить, может дать рекомендации юзеру ("для этого бота понадобится API ключ от OpenAI")
- (+) Проверяет существующие стори перед созданием новых — переоткрывает если баг повторяется
- (+) Может закрыть стори как "не нужно" если юзер передумал
- (-) НЕ указывает техническую реализацию
- (-) НЕ декомпозирует на таски
- (-) НЕ взаимодействует с кодом

**Новое поведение — reopen flow**:
1. Юзер: "картинки всё ещё не работают"
2. PO вызывает `list_stories(project_id)` — видит недавно завершённую стори "Fix tarot card images"
3. PO вызывает `reopen_story(story_id, reason="User reports images still broken")` вместо `create_story()`
4. Стори → `IN_PROGRESS`, уходит в `architect:queue` с флагом `is_reopen=true`
5. Architect видит предыдущие таски + их результаты и может создать новые/другие таски

### Architect

**Философия**: Промежуточная нода. Основная задача — нарезать работу так, чтобы Claude (девелопер) не получал гигантские задачи, невозможные за один подход.

**Границы**:
- (+) Хорошо нарезает таски — понимает scope и зависимости
- (+) При reopen — видит предыдущие таски, их события и результаты, создаёт ДРУГИЕ таски с учётом того что уже сделано/не получилось
- (+) Может переоткрыть конкретный таск (`task.reopen()`) если проблема локализована
- (+) Может создать новые таски в рамках существующей стори
- (-) НЕ указывает детали реализации — Claude умнее в этом
- (-) НЕ имеет доступа к интернету или коду — работает с описаниями и спеками

**При reopen стори**:
1. Architect получает `ArchitectMessage` с `is_reopen=true`
2. Вызывает `get_tasks_by_story()` — видит все предыдущие таски + события
3. Анализирует: что получилось, что нет, почему
4. Решает: переоткрыть конкретный таск ИЛИ создать новый с уточнённым описанием
5. Новые таски содержат контекст: "Предыдущая попытка (task-XXX) скачала placeholder файлы для Minor Arcana. Нужно использовать другой источник изображений."

### Developer (Engineering Worker)

**Философия**: Самая автономная нода. Получает задачу — решает как хочет. Если не может — тормозит и говорит.

**Промпт (концепция)**:
```
Вот задача: {task.description}
Вот acceptance criteria: {task.acceptance_criteria}
Вот доки фреймворка: AGENTS.md
Вот стори если хочешь контекст: {story.description}
Вот предыдущие таски и их результаты: {story_context}

Реализуй задачу. Если что-то не получается — не пытайся зашипить
хоть что-то. Используй `orch report-blocker` чтобы сообщить о проблеме.
```

**Границы**:
- (+) Полная автономия в реализации — выбирает подход, пишет код, тесты
- (+) Имеет доступ к интернету (внутри контейнера), фреймворку, коду проекта
- (+) Может и ДОЛЖЕН сообщать о блокерах через `report-blocker` tool
- (-) НЕ принимает продуктовые решения
- (-) НЕ меняет scope задачи

**Новое поведение — blocker reporting**:
1. Developer скачивает 78 изображений, 56 возвращают 404
2. Developer вызывает `orch report-blocker --reason "56/78 Minor Arcana URLs return 404. Only Major Arcana images are valid."`
3. Task gets `blocker` event → supervisor picks up → publishes to `architect:queue` (or creates manual review request)

---

## Proposal: Reopen + Feedback Flows

### Flow 1: User reports recurring issue (PO → reopen)

```
User: "картинки всё ещё не работают"
  → PO: list_stories() → finds completed story with similar scope
  → PO: reopen_story(story_id, reason)
  → Story: COMPLETED → IN_PROGRESS
  → architect:queue (is_reopen=true)
  → Architect: sees previous tasks + results
  → Architect: creates new tasks OR reopens specific task
  → engineering:queue → Developer (with full context of previous attempt)
```

### Flow 2: Developer hits blocker (Developer → escalation)

```
Developer: 56/78 URLs return 404
  → orch report-blocker "..."
  → TaskEvent(type="blocker", details={reason, context})
  → Task: IN_DEV → BLOCKED
  → Вариант A: supervisor автоматически переназначает в architect:queue
  → Вариант B: task stays BLOCKED, picked up by next dispatcher cycle
  → Architect: reviews blocker, creates fix/alternative task
  → Developer: gets new task OR updated instructions
```

### Flow 3: Post-deploy verification fails (автоматический reopen)

```
Deploy success → smoke test → partial failure detected
  → Story: stays in DEPLOYING (или → COMPLETED с issue flag)
  → PO получает proactive: "Deployed but N issues detected"
  → PO решает: reopen или notify user
```

---

## Что нужно реализовать

### Минимальный скоуп (MVP)

1. **PO: reopen_story tool** — новый tool для PO агента
   - Проверяет existing stories перед create
   - `COMPLETED → IN_PROGRESS` + publish to `architect:queue` с `is_reopen=true`

2. **Architect: reopen-aware decomposition**
   - `ArchitectMessage` получает `is_reopen` flag
   - Architect видит предыдущие таски + события через `get_tasks_by_story()`
   - Может: создать новые таски, переоткрыть существующие (`POST /tasks/{id}/reopen`)

3. **Developer: report-blocker tool** (orchestrator-cli)
   - `orch report-blocker --reason "..."` → `POST /api/tasks/{id}/events` с type=`blocker`
   - Task → `BLOCKED`
   - Worker останавливается (не коммитит полурабочий код)

4. **PO prompt update** — инструкция проверять `list_stories()` перед `create_story()`

### Дальнейшее развитие

5. **Автоматическое переназначение blocked тасков** — supervisor или dispatcher подхватывает BLOCKED таски и отправляет в architect:queue
6. **CI gate per story** (#1004) — тесты после каждой итерации стори, не после каждого таска
7. **Developer ↔ Architect dialog** — полноценный двусторонний канал (сложнее, может быть overkill)

---

## Open Questions

1. **Blocker auto-escalation**: Должен ли BLOCKED таск автоматически уходить к архитектору, или ждать ручного решения? Автоматика проще но может зациклиться.

2. **Reopen limit**: Сколько раз можно переоткрывать стори? Нужен ли max_reopens чтобы не зациклиться?

3. **Partial success**: Что делать если Developer зарепортил блокер но часть работы сделал? Коммитить частичный результат или откатить?

4. **Architect intelligence**: Архитектор сейчас на sonnet. Достаточно ли его для анализа "что пошло не так и как по-другому"? Или для reopen нужен opus?

---

## Action Items

- → **task-ce845712**: "Story/Task reopen flow with user_report field" (PO reopen tool + Architect reopen + user_report through pipeline) — CREATED
- → idea: "orchestrator-cli: add report-blocker command + BLOCKED task transition" (separate task, developer escalation)
- → idea: "Automatic blocker escalation: BLOCKED task → architect:queue via dispatcher"
- → idea: "Post-deploy smoke test verification with automatic reopen on partial failure"
- → backlog #1004 (CI gate per story — enables iterative development within story)
