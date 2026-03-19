# Workspace Re-scaffolding — Auto-recovery When Workspace Missing

> [!WARNING]
> Этот файл автогенерируется командой `make sync`. Не редактируйте вручную — изменения будут перезаписаны.

# Brainstorm: Workspace Re-scaffolding — Auto-recovery When Workspace Missing

> **Дата**: 2026-03-13
> **Контекст**: engineering worker падает с "Scaffolded workspace not found" для проектов с удалённым GC воркспейсом
> **Status**: draft

---

## Current State

### Нормальный флоу (первый запуск проекта)
```
PO создаёт проект (status=DRAFT) + story
  → scaffold_trigger видит DRAFT + stories → публикует в scaffold:queue
  → scaffolder: clone repo, copier, make setup, git push
  → scaffolder: project.status = DRAFT → ACTIVE
  → task_dispatcher: видит status=ACTIVE → диспатчит в engineering:queue
  → engineering worker: спавнит worker с workspace из /data/workspaces/{repo_id}
```

### Защиты, которые уже есть
1. **scaffold_trigger** — публикует в scaffold:queue только для проектов со `status=DRAFT` (scaffold_trigger.py:45)
2. **task_dispatcher** — НЕ диспатчит задачи если `project.status == DRAFT` (task_dispatcher.py:141-145)
3. **architect consumer** — ждёт до 5 мин пока проект станет ACTIVE (architect.py:127-142)
4. **worker-manager** — проверяет что `/data/workspaces/{repo_id}` существует, RuntimeError если нет (manager.py:601-604)

### Проблема — повторная фича для существующего проекта

Когда пользователь просит фичу для проекта который уже `ACTIVE`:

```
Проект: status=ACTIVE (scaffold давно прошёл)
GC удалил /data/workspaces/{repo_id} (старше 35ч)
Пользователь: "добавь команду /revert"
  → PO создаёт story
  → architect: работает (status=ACTIVE, ок) — НО файлового дерева нет на диске!
  → task_dispatcher: видит status=ACTIVE → диспатчит в engineering:queue
  → engineering worker: спавнит worker
  → worker-manager: /data/workspaces/{repo_id} не найден → RuntimeError → FAIL
```

**Ключевой дефект**: для ACTIVE проектов никто не проверяет наличие воркспейса на диске. Все защиты завязаны на `project.status == DRAFT`, а после первого scaffold проект навсегда ACTIVE.

## Problem / Opportunity

Нужен механизм, который при отсутствии воркспейса:
1. Останавливает пайплайн (не даёт дойти до engineering)
2. Автоматически запускает ре-скаффолдинг
3. Возобновляет пайплайн после готовности воркспейса

Причём архитектору тоже нужно дерево проекта для декомпозиции — значит проверка должна быть **до архитектора**.

## Options

### Option A: Проверка в task_dispatcher + re-scaffold trigger

Добавить в `dispatch_todo_tasks()` проверку наличия воркспейса на диске. Если нет — запустить ре-скаффолдинг и не диспатчить.

**Реализация:**
1. В `dispatch_todo_tasks()` после проверки `project.status != DRAFT`:
   - Получить `repo_id` через API
   - Проверить наличие `/data/workspaces/{repo_id}` (или запросить worker-manager по HTTP)
   - Если нет — опубликовать в `scaffold:queue` и пропустить задачу (она будет подхвачена в следующем цикле)
2. Scaffolder уже умеет обрабатывать: clone → copier → setup → push

- (+) Простая реализация, минимум изменений
- (+) Работает на уровне 30-сек цикла — self-healing
- (-) Scheduler не имеет доступа к файловой системе воркспейсов (он в другом контейнере)
- (-) Нужен или HTTP-запрос к worker-manager, или проверка через API/Redis
- (-) Не блокирует архитектора — он тоже может работать без дерева

### Option B: Проверка в engineering consumer + блокировка задачи

Engineering consumer перед спавном проверяет workspace и при отсутствии:
1. Публикует в scaffold:queue
2. Ставит задачу в `blocked` с причиной "waiting_for_scaffold"
3. Task dispatcher видит blocked → пропускает → позже scheduler ре-проверяет

**Реализация:**
1. В `process_engineering_job()` перед `request_spawn()`:
   - Запросить worker-manager `/api/introspect/workspaces/{project_id}/check` (новый эндпоинт)
   - Если workspace нет → publish scaffold:queue, transition task → blocked
2. Новый scheduler task: `check_blocked_scaffold_tasks()` — периодически проверяет blocked задачи, если workspace появился → unblock → re-dispatch

- (+) Проверка происходит максимально близко к месту ошибки
- (-) Задача уже в engineering:queue и consumed — нужна re-enqueue логика
- (-) Добавляет сложность с blocked-статусом
- (-) Не решает проблему архитектора

### Option C: Workspace readiness как поле проекта + единая проверка

Добавить поле `workspace_ready: bool` в проект. GC при удалении воркспейса ставит `workspace_ready=false`. Все проверки завязаны на это поле вместо наличия файлов.

**Реализация:**
1. GC (`garbage_collect_workspaces`): при удалении воркспейса → PATCH project `workspace_ready=false`
2. Scaffolder при успешном scaffold → `workspace_ready=true`
3. `scaffold_trigger`: триггерит scaffold если `status=ACTIVE AND workspace_ready=false`
4. `task_dispatcher`: не диспатчит если `workspace_ready=false`
5. Architect consumer: ждёт `workspace_ready=true`

- (+) Единый источник правды — поле в БД, доступно всем сервисам
- (+) Решает проблему и для архитектора, и для engineering
- (+) GC сам сигнализирует что workspace удалён
- (-) Новое поле в модели проекта → миграция
- (-) GC (worker-manager) должен уметь ходить в API

### Option D: Re-scaffold в worker-manager вместо RuntimeError

Worker-manager при отсутствии workspace сам клонирует репо из GitHub вместо ошибки.

**Реализация:**
1. В `create_worker_with_capabilities()` если workspace нет:
   - git clone репо в `/data/workspaces/{repo_id}`
   - Продолжить нормально

- (+) Минимальная задержка — clone прямо перед стартом
- (+) Нет изменений в pipeline/scheduler
- (-) Worker-manager не должен знать про Git/GitHub (нарушение ответственности)
- (-) Нет copier/make setup — дерево будет неполным для нового проекта
- (-) Для существующего проекта git clone достаточен (код уже на GitHub)
- (+) Для feature/fix это именно то что нужно — свежий клон из main

## Analysis

Ключевое наблюдение: **для `action=create` нужен полный copier+scaffold, а для `action=feature/fix` — достаточно git clone**. Это разные сценарии:

1. **Новый проект (create)**: workspace ещё не существует, нужен полный scaffold (copier + make setup + git push). Текущий флоу уже работает через DRAFT → scaffold → ACTIVE.

2. **Фича/фикс для существующего проекта (feature/fix)**: код уже на GitHub, workspace мог быть удалён GC. Нужен просто `git clone` — полный re-scaffold не нужен и даже вреден (copier перезапишет изменения).

## Recommendation

**Гибрид Option D (для feature/fix) + Option C lite (для create)**:

### Для feature/fix (основной сценарий проблемы):
Worker-manager при отсутствии workspace делает `git clone` из GitHub. Это самый чистый подход:
- Worker-manager уже знает про repo (получает repo_id)
- Клон — это идемпотентная операция
- Не нужна координация между сервисами
- **НО**: worker-manager не знает git URL. Нужно передавать его вместе с repo_id, или хранить в Redis.

Реально проще: **engineering consumer** уже знает git URL (он его резолвит в developer node). Передать URL через worker:commands вместе с repo_id.

### Для полного re-scaffold (если repo ещё не на GitHub):
Оставить текущий DRAFT-механизм. Это edge case и текущее решение работает.

### Конкретный план:

1. **worker-manager consumer**: при создании воркера, если workspace не найден и передан `git_url` → git clone → продолжить
2. **engineering consumer**: передавать `git_url` в worker:commands вместе с repo_id
3. **Опционально**: GC ставит флаг в Redis при удалении workspace, чтобы engineering consumer мог залогировать предупреждение

## Action Items

- → new task: "Worker-manager: git clone fallback при отсутствии scaffolded workspace для feature/fix задач"
  - worker-manager consumer: при `scaffolded_exists=False` и наличии `git_url` → clone repo → продолжить
  - engineering consumer: передавать `git_url` в spawn request
  - Тест: unit test для fallback-клона
- → idea: "GC notification — при удалении workspace ставить флаг в Redis/API для observability"
- → idea: "Architect workspace check — архитектору тоже нужно дерево, но он берёт его из project.config.tree (DB), не с диска — проверить что это работает"

