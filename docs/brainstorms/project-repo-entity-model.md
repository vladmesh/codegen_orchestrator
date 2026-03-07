# Brainstorm: Project, Repository, и Entity Model оркестратора

> **Дата**: 2026-03-07
> **Контекст**: Продумать сущности оркестратора: что такое проект, репозиторий, как связаны с задачами. Переименование WorkItem→Task, Task→Run. Роли агентов и уровни абстракции.
> **Status**: done
> **Связано**: [orchestrator-v2-task-management.md](orchestrator-v2-task-management.md), [epic-decomposition.md](epic-decomposition.md), [qa-node.md](qa-node.md), [qa-on-prod-server.md](qa-on-prod-server.md)

---

## Decisions (из дискуссии)

### D1: Переименование сущностей

| Было | Стало | Что это |
|------|-------|---------|
| WorkItem | **Task** | Конкретная задача в одном репо, выполнимая за один вызов Claude Code CLI |
| Task | **Run** | Runtime-исполнение: engineering run, deploy run, CI fix run |

### D2: Иерархия сущностей

```
Project                          — продукт (то что юзер видит как единое целое)
  └── Story                      — что хочет юзер (product-level, от PO)
        └── Task                 — конкретная работа: один репо, один Claude Code вызов
              └── Run            — исполнение задачи (runtime, минуты/часы)
```

**Task = atomic unit of work.** Никаких cross-repo тасок, никаких тасок на десятки шагов. Если задача слишком сложная — она декомпозируется на несколько Task'ов. Декомпозиция — ответственность Architect'а.

### D3: Продуктовые vs технические сущности

**Продуктовые** (видны пользователю через PO):
- **Project** — продукт целиком
- **Story** — user story, запрос на фичу/фикс
- **Epic** — группа связанных Stories (потом, как parent_story_id)

**Технические** (генерируются автоматически, пользователь не видит):
- **Repository** — git-репозиторий проекта
- **Task** — единица работы для Developer'а
- **Run** — runtime-исполнение Task'а
- **Spec** — спецификация репозитория (project-spec.yaml)

### D4: Роли агентов — уровни абстракции

```
User
  ↕ (разговор)
PO                    — Product layer: Project, Story, Epic
  ↕ (stories)
Architect             — Spec layer: декомпозиция Story→Tasks, валидация спек
  ↕ (tasks + specs)
Developer             — Code layer: spec→code, code→spec updates
  ↕ (deployed code)
Tester                — Verification: story→tests→run on prod
  ↕ (результат)
PO → User
```

Каждая роль — мост между двумя уровнями абстракции. Никто не перепрыгивает через уровень:
- **PO** не знает про репозитории, таски, спеки. Оперирует проектами и сторями.
- **Architect** не читает код. Оперирует спеками, сторями, тасками.
- **Developer** не общается с юзером. Получает таску + спеку, пишет код.
- **Tester** не знает как устроен код. Получает story + URL, тестирует как юзер.

---

## Current State

### Что есть сейчас

```
Project (БД)
  ├── id, name, owner_id
  ├── github_repo_id, repository_url    — 1:1 с GitHub repo
  ├── config.description                — всё описание в одном блобе
  ├── config.modules, config.secrets
  ├── project_spec                      — .project-spec.yaml (если есть)
  └── status                            — draft → scaffolding → ... → active

WorkItem (БД, будет переименован в Task)
  ├── project_id (FK → projects)
  ├── title, description, status, priority
  └── plan, current_iteration, max_iterations

Task (БД, будет переименован в Run)
  ├── type: "engineering" | "deploy"
  ├── status: queued → running → completed/failed
  └── task_metadata, result, error_message
```

**Отсутствуют: Story, Repository (отдельная), Architect, Tester.**

### Текущий flow и что сломано

```
User: "Хочу бота для курсов валют"
  → PO: наводящие вопросы
  → PO: create_project(description="...всё в одном блобе...")
  → trigger_engineering(action="create")
  → Один worker получает весь description → делает всё за раз
```

Проблемы:
1. **Нет Story.** Описание проекта/фичи = текст в config.description. Не сущность, нет статуса, нет acceptance criteria.
2. **Нет декомпозиции.** Один worker получает всё. Для todo-api ок, для сложного проекта — захлебнётся.
3. **Нет спеки как абстракции для планирования.** Architect'у нечего читать кроме description.
4. **1 Project = 1 Repository.** Нельзя трекать задачи в разных репо одного проекта.

---

## Target: Entity Model

### Project

Продукт. То что пользователь видит как единое целое.

```python
class Project(Base):
    id: Mapped[str]                     # "todo-api"
    name: Mapped[str]                   # "Todo API"
    description: Mapped[str]            # Краткое описание (для UI, не для разработки)
    status: Mapped[str]                 # draft | active | archived
    owner_id: Mapped[int]              # FK → users
    # Убрано: repository_url, github_repo_id (переехало в Repository)
    # config.description → разработка строится на Stories, не на description
```

### Repository

Git-репозиторий. 1 Project → N Repositories.

```python
class Repository(Base):
    id: Mapped[str]                     # "repo-a1b2c3d4"
    project_id: Mapped[str]            # FK → projects
    name: Mapped[str]                   # "todo-api" (human-readable)
    git_url: Mapped[str]               # "https://github.com/org/repo"
    provider_repo_id: Mapped[int|None] # GitHub numeric ID
    role: Mapped[str]                   # "primary" | "dependency"
    is_managed: Mapped[bool]           # True = создан оркестратором
```

Типы по ownership:
- **Managed** — создан оркестратором в нашей org (GitHub App авторизация)
- **Connected** — пользователь дал доступ (будущее: GitHub App install / PAT)
- **External** — read-only зависимость (не пушим)

Git provider abstraction (gitlab, bitbucket) — YAGNI, добавим когда появится второй провайдер.

### Story

User Story. Что хочет пользователь. Создаётся PO по итогам разговора.

```python
class Story(Base):
    id: Mapped[str]                     # "story-a1b2c3d4"
    project_id: Mapped[str]            # FK → projects
    parent_story_id: Mapped[str|None]  # FK → stories (self-ref, для Epic-like группировки)
    title: Mapped[str]                  # "Добавить кнопку статистики"
    description: Mapped[str]           # Полное описание от PO
    acceptance_criteria: Mapped[str|None]  # Когда считать done
    status: Mapped[str]                # created | in_progress | completed | archived
    created_by: Mapped[str]            # "po" | "user" | "system"
```

**parent_story_id** — nullable self-ref. Позволяет группировать Stories (Epic = Story с children). Не отдельная сущность, просто parent.

### Task

Конкретная работа. Один репо, один Claude Code вызов. Генерируется Architect'ом из Story.

```python
class Task(Base):                       # (бывший WorkItem)
    id: Mapped[str]                     # "task-a1b2c3d4"
    story_id: Mapped[str]              # FK → stories
    repository_id: Mapped[str]         # FK → repositories (NOT NULL — всегда конкретный репо)
    title: Mapped[str]                  # "Добавить эндпоинт /stats"
    description: Mapped[str]           # Техническое описание для Developer'а
    status: Mapped[str]                # backlog | todo | in_dev | done | failed
    priority: Mapped[int]              # Порядок выполнения внутри Story
    plan: Mapped[str|None]             # Шаги плана (от Architect'а)
    current_iteration: Mapped[int]     # Номер попытки
    max_iterations: Mapped[int]        # Лимит
```

**repository_id NOT NULL** — Task всегда в конкретном репо. Это ключевое правило.

### Run

Runtime-исполнение Task'а. (Бывший Task.)

```python
class Run(Base):                        # (бывший Task)
    id: Mapped[str]                     # "run-a1b2c3d4"
    task_id: Mapped[str|None]          # FK → tasks (nullable для обратной совместимости)
    type: Mapped[str]                   # "engineering" | "deploy" | "test"
    status: Mapped[str]                # queued | running | completed | failed
    iteration: Mapped[int|None]        # Какая итерация Task'а
    result: Mapped[dict]               # commit_sha, logs, etc.
    error_message: Mapped[str|None]
```

---

## Target: Flow

### Создание проекта

```
User: "Хочу бота для курсов валют с Redis-кешем"
  ↓
PO: уточняет требования, собирает описание
  ↓
PO tools:
  1. create_project(name="Currency Bot", description="Telegram бот для курсов валют")
     → Project создан
  2. create_story(project_id, title="Создать бота с базовым функционалом",
                  description="...", acceptance_criteria="...")
     → Story создана, status: created
  3. start_story(story_id)
     → status: in_progress
  ↓
Scheduler / PO tool → запускает Architect
  ↓
Architect:
  input: Story + Project specs (пустые для нового проекта)
  1. Решает: нужна декомпозиция? (для create — обычно одна Task)
  2. Создаёт Repository(role="primary", is_managed=True)
  3. Генерит Tasks:
     - Task "Scaffold + implement currency bot" (repo=primary)
  4. Обновляет/создаёт начальную спеку проекта
  ↓
Developer: берёт Task → scaffold (copier) → код → push
  ↓
Deploy: автоматически после CI green
  ↓
Tester: получает Story.acceptance_criteria + deployed URL
  → генерит тесты, прогоняет на проде (Claude Code на сервере)
  → pass → Story.status = completed
  → fail → назад в Architect (новая Task на фикс)
  ↓
PO → User: "Готово!"
```

### Добавление фичи

```
User: "Добавь кнопку статистики"
  ↓
PO:
  1. list_stories(project_id) → видит историю
  2. create_story(project_id, title="Кнопка статистики", ...)
  3. start_story(story_id)
  ↓
Architect:
  input: Story + Project spec + Repo specs + completed Stories
  1. Читает спеку → понимает архитектуру БЕЗ чтения кода
  2. Решает сложность:
     - Простая фича → 1 Task
     - Сложная → N Tasks с порядком
  3. Генерит Tasks с техническими описаниями
  ↓
Developer: Task по Task, spec-first где возможно
  После каждого Task → Architect сверяет спеки со Story
  ↓
Tester → PO → User
```

### Сложная фича (декомпозиция)

```
User: "Добавь авторизацию, Redis-кеш и админку"
  ↓
PO: может разбить на 3 Stories, или создать одну большую
  ↓
Architect:
  Story "Авторизация + Redis + админка"
  → Task 1: "Добавить JWT авторизацию" (repo=primary, priority=0)
  → Task 2: "Подключить Redis-кеш" (repo=primary, priority=1)
  → Task 3: "Админ-панель" (repo=primary, priority=2)

  Каждая Task — один Claude Code вызов, чёткий scope.
  Developer не знает про остальные Tasks — делает свою.
```

---

## Architect: что получает на вход

Architect не читает код. Оперирует абстракциями:

| Абстракция | Источник | Что даёт |
|------------|----------|----------|
| **Story** | PO (из разговора с юзером) | ЧТО нужно сделать, acceptance criteria |
| **Project spec** | `.project-spec.yaml` / project_spec в БД | Архитектура: сервисы, домены, модели, события |
| **Repo README** | README.md каждого репо | Tech stack, как запустить, структура |
| **Completed Stories** | БД (stories со status=completed) | Контекст: что уже сделано |
| **Repo manifest** | Repository записи в БД | Какие репо, их роли |

**Спека — центральная абстракция для Architect.** Замкнутый цикл:

```
Story → Architect (reads specs) → Tasks
  → Developer executes Task → code + spec updates
    → Architect validates: specs match Story?
      → yes → next Task / Tester
      → no → ещё Task на фикс спеки/кода
```

Спеки обновляются Developer'ом как часть работы:
- **Spec-first** (где возможно): Developer сначала обновляет спеку, потом `service_template generate` генерит код
- **Code-first** (где нельзя spec-first): Developer пишет код, потом обновляет спеку. В промпте Developer'а явно: "обнови спеки после изменений"
- **Страховка**: после завершения Task Architect сверяет что спеки отвечают Story

---

## Tester

Отдельная нода после деплоя. Не юнит-тесты (это Developer), а acceptance testing — проверка Story как пользователь.

**Подробно**: см. [qa-node.md](qa-node.md) и [qa-on-prod-server.md](qa-on-prod-server.md) (bs-a7153455, bs-2febe24a).

Ключевое:
- Вход: Story.acceptance_criteria + deployed URL/bot handle + credentials
- Генерит test steps из acceptance criteria
- Выполняет: httpx / Telethon / Playwright MCP
- Запускается на прод-сервере рядом с проектом (сетевая близость, доступ к логам)
- pass → Story.status = completed → PO уведомляет юзера
- fail → описание бага → Architect создаёт Task на фикс

---

## Repository модель

### Project → Repository (1:N)

```
Project: "Currency Bot"
  └── Repository: currency-bot (role: primary, managed)
        git: github.com/project-factory-organization/currency-bot

Project: "codegen-orchestrator" (наша разработка)
  ├── Repository: orchestrator (role: primary, managed)
  └── Repository: service-template (role: dependency, external)
```

### Task.repository_id — NOT NULL

Task всегда привязана к конкретному репо. Нет cross-repo тасок. Если Story затрагивает 2 репо — Architect создаёт отдельные Tasks per repo.

### Хранение workspace

Текущий подход (Strategy 2) — нормальный с доработкой:
- Workspace key: `repository_id` вместо `project_id`
- При старте Task: `git fetch && git reset --hard origin/main`
- GC: 24h без активности

Менять workspace management — отдельная задача, после того как Repository модель устоится.

---

## Миграция

### Phase 1: Rename + новые модели

1. **Rename**: WorkItem → Task, Task → Run (в коде, API, БД)
2. **Story model** + API + миграция
3. **Repository model** + API + миграция существующих Project.repository_url → Repository
4. **Task.story_id** (FK → stories) + **Task.repository_id** (FK → repositories)
5. **Story.parent_story_id** (self-ref FK для epic-like группировки)

### Phase 2: Architect node

1. Architect input: Story + specs + repos + history
2. Architect output: Tasks with repository assignments
3. Spec validation loop после каждого Task

### Phase 3: Tester node

1. Story.acceptance_criteria → test generation
2. Execution на prod-сервере
3. Pass/fail → обратная связь в Story

---

## Открытые вопросы

### Q1: Нейминг Architect

Architect, Tech Lead, Planner? Architect кажется правильным — системное мышление, спеки, декомпозиция. Но пересекается с "software architect" (который проектирует с нуля). У нас скорее "decomposes and validates". Оставляем Architect?

### Q2: Story для create_project

Создание проекта — это Story? "Создать бота для курсов валют" — звучит как Story. Architect генерит Tasks (scaffold + implement). Или create = особый flow без Story?

### Q3: Где живёт спека для нового проекта?

Для нового проекта спеки ещё нет. Architect генерит начальную спеку из Story description? Или Developer при scaffold создаёт спеку (copier генерит .project-spec.yaml)?

### Q4: PO создаёт одну Story или несколько?

"Авторизация + Redis + админка" — PO создаёт 1 Story (и Architect декомпозирует на Tasks)? Или PO создаёт 3 Stories? Кто решает гранулярность на продуктовом уровне?

### Q5: Repository модель — нужна сейчас?

Для dogfooding (наша разработка) достаточно текстового поля `repo` в Task. Полноценная Repository модель нужна для оркестратора (managed repos, workspace management). Делать сейчас или отложить?

---

## Action Items

### Фундамент
- → new task: "Rename WorkItem→Task, Task→Run" — модели, API, миграции, тесты, скиллы. Крупная задача, нужна декомпозиция.
- → new task: "Story model + API" — новая сущность, CRUD, parent_story_id для epic-like группировки.
- → new task: "Repository model + migration" — Alembic migration, CRUD API, миграция Project.repository_url → Repository.

### Architect
- → new task: "Architect node design" — input/output contract, spec-based decomposition, validation loop. Начать с brainstorm.

### Будущее
- → idea: "Workspace per repository" — worker-manager workspace key: project_id → repository_id.
- → idea: "Git provider abstraction" — multi-provider auth. Когда появится второй провайдер.
- → idea: "Bring-your-own-repo flow" — подключение пользовательского репо. Phase 4+.

### Далёкое будущее
- → idea: "Spike task type" — когда Architect не может декомпозить Story, создаёт Task type=spike. Developer исследует кодовую базу, пишет отчёт (не код). Architect получает отчёт → пробует декомпозить снова. Альтернатива: Story.status=needs_clarification → PO уточняет у юзера.
- → idea: "Multi-model technical brainstorm" — несколько LLM обсуждают сложное техническое решение (structured debate). Дорого, медленно, но потенциально полезно для архитектурных решений где один model застревает. Исследовать когда/если single-model Architect упрётся в потолок качества декомпозиции.
