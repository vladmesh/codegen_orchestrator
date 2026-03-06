# Brainstorm: Task Description Flow

> **Дата**: 2026-03-07
> **Контекст**: Description из trigger_engineering теряется для action=create — воркер получает пустую спеку
> **Status**: done

---

## Current State

### Поля проекта в БД (shared/models/project.py)

| Поле | Тип | Откуда заполняется | Назначение |
|------|-----|-------------------|------------|
| `id` | str | `create_project` tool (uuid[:8]) | PK |
| `name` | str | PO → `create_project` | Имя проекта |
| `status` | str | Меняется по ходу пайплайна | draft → scaffolding → active → ... |
| `config` | JSON | PO → `create_project` | Мешок всего (см. ниже) |
| `config.description` | str | PO → `create_project(description=)` | Короткое описание ("Telegram bot for X") |
| `config.detailed_spec` | str? | PO → `create_project(detailed_spec=)` | Полная спека (markdown) |
| `config.modules` | list | PO → `create_project(modules=)` | ["backend", "tg_bot"] |
| `config.secrets` | dict | PO → `set_project_secret` | Зашифрованные секреты |
| `config.env_hints` | dict | PO → `set_project_secret(hint=)` | Подсказки для воркера по env vars |
| `config.entry_points` | list | PO → `create_project(entry_points=)` | ["telegram", "api"] |
| `repository_url` | str? | engineering_worker после создания repo | URL GitHub репо |
| `github_repo_id` | int? | scheduler (github_sync) | GitHub repo ID |
| `project_spec` | JSON? | PO → `create_project_spec_yaml` tool | Машиночитаемая спека из .project-spec.yaml |
| `owner_id` | int | API middleware (X-Telegram-ID) | FK на users |

### Поля в EngineeringMessage (Redis queue)

| Поле | Тип | Откуда |
|------|-----|--------|
| `task_id` | str | PO → `trigger_engineering` |
| `project_id` | str | PO |
| `user_id` | str | PO (из configurable) |
| `action` | str | PO: "create" / "feature" / "fix" |
| `description` | str? | PO → `trigger_engineering(description=)` |
| `skip_deploy` | bool | PO |
| `callback_stream` | str | PO (hardcoded po:input) |

### Как description попадает в TASK.md

#### action=create
```
PO вызывает trigger_engineering(project_id, description="полное описание")
  → EngineeringMessage.description = "полное описание"
    → engineering_worker: description = job_data["description"]
      → subgraph_input["description"] = "полное описание"
        → developer node: feature_description = state["description"]  # = "полное описание"
          → _build_task_message(description=project_description, feature_description=feature_description)
            → _build_create_task(description=config.description, project_spec=...)
              ┌─────────────────────────────────────────────┐
              │ Description: {config.description}           │  ← КОРОТКОЕ "Telegram bot for X"
              │ Detailed Spec: {project_spec.detailed_spec} │  ← N/A (PO не заполняет)
              │ feature_description ИГНОРИРУЕТСЯ             │  ← ПОТЕРЯ!
              └─────────────────────────────────────────────┘
```

#### action=feature/fix
```
PO вызывает trigger_engineering(project_id, action="feature", description="добавь кнопку X")
  → та же цепочка...
    → _build_feature_task(feature_description="добавь кнопку X")
      ┌─────────────────────────────────────────────┐
      │ What To Do: {feature_description}           │  ← ОК, используется
      │ Description: {config.description}            │  ← контекст проекта
      └─────────────────────────────────────────────┘
```

## Problem

**Для action=create description теряется дважды:**

1. **PO не заполняет `detailed_spec` при `create_project`**. Промпт говорит ему собрать описание и передать в `trigger_engineering(description=)`, но НЕ говорит класть его в `create_project(detailed_spec=)`.

2. **`_build_create_task` игнорирует `feature_description`** (= state["description"]). Использует только `config.description` (короткое) и `project_spec.detailed_spec` (пустое).

**Результат**: воркер Claude получает TASK.md с `Detailed Spec: N/A` и однострочным description. Вынужден сам додумывать что строить.

### Дополнительная проблема: config как мешок

`config` — JSON без схемы. Туда кладётся всё: description, detailed_spec, modules, secrets, env_hints, entry_points, maintenance_request. Нет валидации, легко потерять поле.

## Options

### Option A: Сохранять description в project.config.detailed_spec при create

PO передаёт description в `trigger_engineering`. Но ДО этого вызова, при `create_project`, PO уже мог бы передать `detailed_spec`. Проблема в том, что промпт не инструктирует PO это делать.

**Изменения:**
- Обновить PO промпт: передавать собранное описание в `create_project(detailed_spec=...)`
- Или: в `trigger_engineering` перед отправкой в queue — сохранять description в project.config.detailed_spec через API PATCH

(+) Description персистится в БД — можно перезапустить задачу без потери
(+) Минимальные изменения кода
(-) Дублирование: description в queue И в БД
(-) Зависимость от того, что PO правильно заполнит оба поля

### Option B: Пробрасывать description из queue в _build_create_task

`_build_create_task` уже получает `description` (= config.description). Добавить параметр `feature_description` и использовать его как Detailed Spec если project_spec.detailed_spec пустой.

**Изменения:**
- `_build_create_task` принимает `feature_description` и вставляет в Detailed Spec
- Или: `_build_task_message` подставляет feature_description в project_spec["detailed_spec"] перед вызовом

(+) Простой фикс в одном месте (developer.py)
(+) Не зависит от промпта PO
(-) Description не персистится — при retry задачи оно потеряется (берётся из queue, а queue уже consumed)

### Option C: Комбинация — сохранять + пробрасывать

1. `trigger_engineering` сохраняет description в `project.config.detailed_spec` через API PATCH (если не пустое)
2. `_build_create_task` использует `feature_description` как fallback для detailed_spec

(+) Description персистится + не теряется в developer node
(+) Retry-safe: при перезапуске description уже в БД
(+) Не зависит от промпта PO (но промпт тоже стоит поправить)
(-) Чуть больше изменений

### Option D: Убрать detailed_spec, всегда использовать description из queue

Отказаться от `config.detailed_spec` как отдельного поля. Всегда передавать полное описание через queue message, developer node всегда использует `state["description"]`.

(+) Единый поток данных
(-) Потеря при retry
(-) Ломает обратную совместимость

## Recommendation

**Option C** — комбинированный подход:

1. В `trigger_engineering` (po/tools.py): если `description` не пустой — PATCH project.config.detailed_spec
2. В `_build_create_task` (nodes/developer.py): использовать `feature_description` как fallback для `project_spec.detailed_spec`
3. В промпте PO (po/prompts.py): добавить инструкцию передавать detailed_spec в create_project

Это даёт:
- **Немедленный фикс**: даже без изменения промпта description из queue дойдёт до TASK.md
- **Персистентность**: description сохраняется в БД для retry
- **Промпт**: PO учится заполнять detailed_spec сразу при создании проекта

## Action Items

- → new task: "Fix description loss in create flow — persist in DB + pass to TASK.md (Option C)"
- → idea: "Типизировать project.config — заменить свободный JSON на Pydantic модель с валидацией"
