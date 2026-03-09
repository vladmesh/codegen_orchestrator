# Brainstorm: Architect Context & Worker Knowledge — Service Template Awareness

> **Дата**: 2026-03-09
> **Контекст**: Architect не знает про service-template, создаёт избыточные таски. Воркер не получает документацию фреймворка.
> **Status**: done
> **Result**: Full pipeline spec → [docs/PIPELINE_V2.md](../PIPELINE_V2.md)

---

## Current State

### Architect
- Получает: story description, project config (modules, description, detailed_spec)
- Инструмент `get_project_spec` возвращает project dict, но **ничего про service-template**
- Промпт generic: "decompose into tasks, foundational work first"
- Результат: 5 задач на простого бота (utility function, bot infra, message handler, error handling, deployment config)

### Worker (Developer Node)
- TASK.md упоминает "scaffolded with copier from service-template" — но **без деталей**
- Generic INSTRUCTIONS.md — ничего про framework, specs, generators
- AGENTS.md из скафолдированного проекта referenced но не инжектится в контекст LLM
- Воркер не знает что: router/handler уже сгенерирован, compose уже есть, CI уже есть

### Scaffolding (Worker-Manager)
- Автоматически: `copier copy service-template workspace --data modules=...`
- Потом: `make setup`
- На выходе: полная инфра (Docker, compose, CI, Makefile, generated routers/handlers/events)

### Timing (ключевая проблема)

Текущий flow:
```
PO → story → architect → tasks → dispatcher → worker → scaffold → implement
```

Scaffolding происходит **внутри воркера** при первой задаче. В момент работы architect-а
проекта ещё нет — нет tree, нет specs, нет ничего кроме описания.

## Problem

**Знание разорвано на трёх уровнях:**

1. **Architect** думает что проект будет строиться с нуля → создаёт задачи на инфру, конфиги, ошибко-обработку
2. **Worker** получает абстрактное описание → не знает что уже сгенерировано и что генерировать не нужно
3. **Scaffolding** делает 80% работы автоматически → но ни architect ни worker об этом не знают

**Конкретный пример**: "Create string reverser bot"
- Architect создал 5 задач, из которых 3 (infra, error handling, deployment config) уже покрыты scaffolding
- Нужно было: 1 задача "implement reverse logic in message handler" (всё остальное из коробки)

## Решение: Scaffold → Architect → Worker

### Идея

Вынести scaffold ДО architect-а. Architect получает tree + specs уже существующего проекта
и создаёт задачи только на дифф между текущим состоянием и желаемым.

### Новый flow

```
PO → story → scaffold (подготовка workspace) → architect (видит tree + specs) → tasks → worker
```

Для architect это меняет постановку:
- Было: "создай бота с нуля" → 5 задач
- Стало: "в существующем проекте с этой структурой допиши то, что описано в story" → 1–2 задачи

Для feature/fix задач проект уже существует → ничего не меняется, architect сразу видит tree.

### Что architect получает

1. **Story** — "пользователь хочет чтобы бот разворачивал строки"
2. **Project specs** — modules, description, detailed_spec
3. **Tree** скафолдированного проекта (новый tool или расширение `get_project_spec`)
4. **AGENTS.md** — из проекта, описывает паттерны, генераторы, что не трогать

### Что architect НЕ должен делать

- Создавать задачи на инфру (Docker, compose, CI) — уже есть
- Создавать задачи на error handling / logging — это часть реализации каждой задачи
- Углубляться в детали реализации — это задача воркера, у которого AGENTS.md

### Промпт architect-а (суть)

```
You receive an existing scaffolded project. Your job is to create tasks
for the DIFFERENCE between what exists and what the story requires.

Do NOT create tasks for infrastructure, deployment, CI/CD, or boilerplate —
these are handled by scaffolding. Focus only on business logic.

Do NOT specify implementation details — the worker has project documentation
(AGENTS.md) and will figure out how to implement.
```

### Что worker получает

1. **TASK.md** — описание задачи от architect-а
2. **AGENTS.md** — уже в проекте, Claude Code читает автоматически
3. **Скафолдированный проект** — уже готов (scaffold был до architect-а)

Worker видит AGENTS.md → знает про генераторы, specs, что не трогать → пишет только бизнес-логику.

## Implementation: Scaffold до Architect

### Scaffolder — отдельный микросервис

Scaffold — это подготовка репозитория, а не часть worker lifecycle. Раньше scaffold
был вшит в worker-manager (copier + make setup при создании worker-а). Но теперь
scaffold должен происходить **до architect-а**, задолго до создания worker-а.

Смешивать эту ответственность с worker-manager неправильно: worker-manager создаёт
и управляет контейнерами воркеров, а scaffold — это про репозиторий и файловую структуру.

### Сервис `scaffolder`

**Ответственность**: подготовка репозитория нового проекта.

**Триггер**: создание нового repository в проекте (сейчас 1:1 с проектом, но модель
поддерживает multi-repo).

**Что делает**:
1. Создаёт GitHub repo (если не создан)
2. `copier copy service-template workspace --data modules=...`
3. `make setup` (framework generate)
4. `git push`
5. Сохраняет tree в `repository.config.tree` (или `project.config.tree`)
6. Обновляет `project.status = scaffolded`

**Зависимости** (минимальные):
- `uv` (для copier via `uv tool`, venv, pip install, sync)
- `git`
- `httpx` (API client)
- `redis` (consumer)
- `structlog`
- НЕ нужен: Docker SDK, docker.sock, langgraph, LLM

**Почему Docker не нужен**: `make setup` — чисто Python-операции (uv venv, uv pip install,
framework.generate, ruff format). Scaffolder выполняет copier и make setup напрямую
в своей файловой системе, работая с workspace volume на хосте.

**Workspace**: scaffolder работает с папкой на диске, привязанной к `repo_id`.
Эта же папка потом маунтится в worker-контейнер как volume.
Worker-manager при создании worker-а подхватывает уже готовый workspace.

### Новый pipeline

```
PO creates project + repo
  → scheduler видит новый repo без scaffold
  → scaffold:queue → scaffolder service
  → copier + make setup + git push
  → project.status = scaffolded, tree сохранён

PO creates story
  → architect:queue → architect (читает tree + specs из API)
  → создаёт задачи на дифф

dispatcher → engineering:queue → worker
  → worker-manager подхватывает готовый workspace volume по repo_id
  → worker видит AGENTS.md, уже скафолдированный проект → пишет бизнес-логику
```

### Worker-manager: что меняется

Worker-manager **перестаёт делать scaffold**. Вместо этого:
1. Получает `repo_id` (или workspace path) в команде на создание worker-а
2. Маунтит готовую папку в контейнер
3. Клонирует/пуллит если нужно (workspace уже есть на диске)

Scaffold-логика (`_run_scaffold_phase`) выносится из worker-manager в scaffolder.

### Tree для architect-а

Scaffolder после scaffold сохраняет tree в DB (через API).
Architect читает через `get_project_spec` — tree приходит в project/repo config.

Для feature/fix stories на существующем проекте — tree обновляется после каждого
успешного task (worker пушит → webhook → API обновляет tree). Или architect дёргает
GitHub API напрямую через tool `get_repo_tree`.

### Workspace volumes

Каждый repo получает persistent volume/папку на хосте:
```
/data/workspaces/{repo_id}/  — рабочая директория
```

Scaffolder создаёт и наполняет. Worker-manager маунтит в контейнер.
Worker коммитит и пушит → workspace актуален.

При story-level worker reuse (#1002) — workspace живёт между задачами.

## Open Questions

1. Workspace на хосте vs named Docker volume? (хост проще для debug)
2. Как обновлять tree после каждой задачи? Webhook → API? Или worker пушит → scaffolder обновляет?
3. AGENTS.md — хватит ли его воркеру, или нужны ещё template docs?
4. Scaffolder нужен только для service-template, или абстрагируем под любой template?

## Action Items

- → new task: "Create scaffolder microservice — copier + make setup + git push, consumes scaffold:queue"
- → new task: "Move scaffold logic out of worker-manager into scaffolder"
- → new task: "Scheduler: trigger scaffold on new repo, architect after scaffold done"
- → new task: "Architect tool: get_project_tree (reads tree from API/DB)"
- → new task: "Architect prompt: 'create tasks for diff, not from scratch'"
- → new task: "Worker-manager: mount workspace volume by repo_id instead of scaffold"
- → idea: "Update tree in DB after each successful task (webhook or worker event)"
- → idea: "Auto-task bypass for trivial projects (skip architect entirely)"
