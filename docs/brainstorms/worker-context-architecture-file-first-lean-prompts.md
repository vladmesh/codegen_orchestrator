---
id: bs-dbb72c3f
status: draft
title: "Worker Context Architecture — File-First, Lean Prompts"
created_at: 2026-03-15T01:37:58.844818Z
---

# Brainstorm: Worker Context Architecture — File-First, Lean Prompts

> **Дата**: 2026-03-15
> **Контекст**: Текущий -p промпт раздут, audit-инструкции теряются, контекст между тасками неэффективен
> **Status**: draft

---

## Current State

### Как сейчас контекст попадает к воркеру

```
Story.description
  → Architect (LLM) → создаёт Task(title, description, acceptance_criteria)
    → Task Dispatcher → обогащает description кумулятивным контекстом из предыдущих тасок
      → Engineering Consumer → строит story_context (таски + события)
        → Developer Node → _build_task_message() → склеивает всё в огромный промпт
          → Worker Spawner → CreateWorkerCommand(instructions, task_content)
            → Worker Manager → пишет CLAUDE.md и TASK.md в контейнер
              → Worker Wrapper → получает prompt из Redis, перезаписывает TASK.md, вызывает:
                claude --dangerously-skip-permissions -p <ВЕСЬ_ПРОМПТ> --output-format json [--resume ID]
```

### Что именно попадает в `-p`

Для create-задачи (реальный пример weather_bot):
```
# Task: Build weather-bot

## Project Specification
**Name**: weather-bot
**Description**: ...
**Modules**: backend,tg_bot

**Detailed Spec**:
<весь detailed_spec из PO — может быть 500+ слов>

## Provided Environment Variables
- TELEGRAM_BOT_TOKEN: ...

## Project Structure (already scaffolded)
The project was scaffolded with copier...
- services/backend/ - main service directory
- /home/worker/TASK.md - detailed requirements
- AGENTS.md - ...
- Makefile - ...

## Implementation
Implement the business logic...
- Read /home/worker/TASK.md for detailed requirements
...

## Story Context (Previous Work)
<список всех предыдущих тасок с событиями>
```

Для feature-задачи — аналогично, только через `_build_feature_task()`.

### Что попадает в CLAUDE.md (INSTRUCTIONS.md)

236 строк статических инструкций:
- Роль, workflow, progress tracking
- Инфраструктура (DB, Redis, make-команды)
- Troubleshooting (DB, DNS)
- Формат REPORT.md (80+ строк шаблона)
- Когда использовать report-blocker

### Что попадает в TASK.md

**Дубликат** `-p` промпта. Worker-wrapper перезаписывает TASK.md содержимым prompt из Redis.
Т.е. TASK.md === `-p` — полная дупликация.

### Что собирается назад

| Артефакт | Как собирается | Статус |
|----------|---------------|--------|
| REPORT.md | wrapper._read_worker_report() → task event | Работает, но удаляет после чтения |
| ~~AUDIT_REPORT.md~~ | Дубликат REPORT.md | Удалить — audit это секции Issues+Suggestions в REPORT.md |
| PROGRESS.md | Остаётся в workspace | Персистит между тасками (через workspace) |
| commit SHA | wrapper._extract_git_commit_sha() | Работает |

---

## Problems

### P1. `-p` переполнен мусором
`-p` содержит ~500-1000 токенов бойлерплейта + story context, который растёт с каждой таской.
Claude CLI загружает его целиком в контекст. Дублирует TASK.md. Дублирует часть CLAUDE.md.

### P2. TASK.md и `-p` — полные дубликаты
Worker-wrapper пишет prompt → TASK.md, потом тот же prompt → `-p`. CLAUDE.md говорит "Read /home/worker/TASK.md" — но Claude уже видит всё через `-p`.

### P3. AUDIT_REPORT.md и REPORT.md — дубликаты одной сущности
REPORT.md определён в INSTRUCTIONS.md (формат: summary, environment, issues, suggestions).
AUDIT_REPORT.md определён в e2e-run skill (формат: "логируй всё что встретишь").
Это одна и та же сущность — "отчёт воркера". Audit — подмножество секций Issues + Suggestions в REPORT.md.
Нужно оставить один файл (REPORT.md), убрать AUDIT_REPORT.md из скилла.

### P4. Story context растёт неограниченно
`_build_story_context()` включает ВСЕ таски + ВСЕ события. К 5-й таске в стори — это может быть 2000+ токенов мусора в `-p`.

### P5. `--resume` делает изоляцию тасок бессмысленной
С `--resume` Claude помнит предыдущие таски из сессии. Значит story_context в промпте — дубликация того что он уже знает. Но без `--resume` он теряет контекст workspace и тратит время на повторное сканирование.

### P6. Нет механизма для story-level инструкций
Нет отдельного поля/файла для инструкций которые должны применяться ко ВСЕМ таскам в стори (audit, code style preferences, etc).

---

## Design Principles (из обсуждения с пользователем)

1. **Весь необходимый контекст должен быть доступен** — но не насильно скормлен
2. **Ничего не пихать в промпт принудительно** — оставить возможность, Claude сам решает
3. **Файловая структура > API** — Claude проще открыть файл, чем дёрнуть URL
4. **Следить за контекстом** — `--resume` полезен, но нужно управлять ростом

---

## Proposal: File-First Context Architecture

### Принцип: `-p` минимален, всё остальное в файлах

```
claude -p "Read /workspace/.story/TASK.md and complete the task described there."
```

Один короткий промпт. Всё остальное — в файловой структуре workspace.

### Файловая структура workspace

```
/workspace/
├── CLAUDE.md                    # Статичные инструкции (как сейчас)
├── .story/                      # Директория стори (managed by orchestrator)
│   ├── TASK.md                  # Текущая задача (описание + acceptance criteria)
│   ├── STORY.md                 # Описание стори + story-level инструкции (audit, etc)
│   ├── CONTEXT.md               # Саммари предыдущих тасок (если нужно)
│   └── reports/                 # Отчёты завершённых тасок (персистят)
│       └── task-12d247fd.md     # REPORT.md предыдущего воркера
├── PROGRESS.md                  # Прогресс текущей задачи (как сейчас)
├── REPORT.md                    # Отчёт по задаче (собирается wrapper'ом)
├── AGENTS.md                    # Паттерны кода (из шаблона)
├── services/                    # Код
└── ...
```

### Детали по каждому файлу

#### `.story/TASK.md` — текущая задача

Минимальный, чистый файл. Содержит ТОЛЬКО:
- Что конкретно сделать (title + description)
- Acceptance criteria
- Ссылки на файлы для контекста (если нужно)

```markdown
# Task: Create backend weather API endpoint

## Description
Implement GET /api/weather/{city} that returns weather data.
Cache responses in PostgreSQL for 30 minutes.

## Acceptance Criteria
- [ ] GET /api/weather/{city} returns JSON with temperature, humidity, description
- [ ] Responses cached in DB for 30 min
- [ ] Returns cached data if fresh enough
- [ ] Tests pass

## References
- See STORY.md for project context
- See CONTEXT.md for previous work in this story
```

Размер: 20-50 строк. Без бойлерплейта.

#### `.story/STORY.md` — контекст стори

Создаётся один раз при старте стори. Содержит:
- Описание проекта и стори
- Story-level инструкции (audit, style preferences)
- Env hints
- Модули

```markdown
# Story: weather-bot initial implementation

## Project
- **Name**: weather-bot
- **Modules**: backend, tg_bot
- **Description**: Telegram bot that returns weather by city

## Story Goal
Build the full weather-bot application: backend API with caching,
Telegram bot with /weather command.

## Environment Variables
- `TELEGRAM_BOT_TOKEN`: Bot token (already configured in .env)

## Story Instructions
These apply to ALL tasks in this story:

### Reporting
Write REPORT.md as usual (see CLAUDE.md). Your report and reports from
previous tasks are saved in .story/reports/ for reference.
```

Claude видит этот файл через CLAUDE.md директиву или читает сам когда TASK.md ссылается на него.

#### `.story/CONTEXT.md` — предыдущая работа

Обновляется перед каждой таской. Компактная сводка:

```markdown
# Completed Tasks

## 1. task-12d247fd — Create backend weather API endpoint (DONE)
- Implemented /api/weather/{city} with cache model
- Commit: abc123
- Note: Router created but may need registration in main app

## 2. task-ce5c2b6e — Create Telegram bot structure (DONE)
- Set up bot with /start and /help commands
- Commit: def456
```

Не включает events, lifecycle transitions и прочий мусор. Только что сделано и что важно знать.

#### `.story/reports/` — отчёты завершённых тасок

Wrapper после сбора REPORT.md копирует его в `.story/reports/task-{id}.md`
вместо удаления. Следующий воркер видит отчёты предшественников (если нужно).
В конце стори — собирается всё как story event.

### Как это меняет pipeline

#### Developer Node (`_build_task_message`)

Вместо склейки огромного промпта — генерирует файлы:

```python
def prepare_worker_files(self, ...) -> dict[str, str]:
    """Return dict of {path: content} to write into workspace."""
    return {
        ".story/TASK.md": self._build_task_file(...),
        ".story/STORY.md": self._build_story_file(...),   # only on first task
        ".story/CONTEXT.md": self._build_context_file(...),
    }
```

#### Worker Manager

Вместо одного TASK.md — пишет все файлы из `prepare_worker_files()`.

#### Worker Wrapper

Минимальный `-p`:
```python
def build_command(self, prompt: str) -> list[str]:
    cmd = ["claude", "--dangerously-skip-permissions",
           "-p", "Read /workspace/.story/TASK.md and complete the task.",
           "--output-format", "json"]
    if self.session_id:
        cmd.extend(["--resume", self.session_id])
    return cmd
```

Или даже проще — `-p` можно вообще убрать если CLAUDE.md содержит "check .story/TASK.md".

#### Сбор артефактов (Wrapper)

```python
def _collect_artifacts(self) -> dict:
    artifacts = {}
    report_path = os.path.join(WORKSPACE_DIR, "REPORT.md")
    if os.path.isfile(report_path):
        with open(report_path) as f:
            artifacts["REPORT.md"] = f.read()
        # Persist for next worker, don't delete
        task_id = data.get("task_id", "unknown")
        dest = os.path.join(WORKSPACE_DIR, ".story", "reports", f"{task_id}.md")
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        shutil.copy2(report_path, dest)
        os.remove(report_path)  # remove from root, kept in .story/reports/
    return artifacts
```

REPORT.md перемещается в `.story/reports/task-{id}.md` — персистит для следующих воркеров.

---

## `--resume` и контекст: trade-offs

### Вариант A: `--resume` по умолчанию (как сейчас)
- (+) Claude помнит workspace, не тратит время на re-scan
- (+) Более эффективное использование времени
- (-) Контекст растёт, autocompact может потерять важное
- (-) Изоляция тасок размыта — ошибки одной таски "заражают" следующую

### Вариант B: Свежая сессия на каждую таску
- (+) Чистый контекст, чистый старт
- (+) Таски реально изолированы
- (-) Тратит 2-5 мин на повторное сканирование проекта
- (-) Теряет "знание" о проекте

### Вариант C: Гибридный (рекомендация)
- **Первая таска в стори** → свежая сессия (чистый старт)
- **Следующие таски** → `--resume` (эффективность)
- **При failure + retry** → свежая сессия (не наследовать ошибки)
- **Новая стори** → свежая сессия (новый контекст)

### Autocompact

Claude CLI в режиме `-p` автоматически делает compaction когда контекст приближается к лимиту. Это работает прозрачно — Claude сам решает что сжать. С `--resume` compaction сохраняет основной контекст но может потерять детали ранних тасок. С файловой архитектурой это менее критично — Claude всегда может перечитать `.story/CONTEXT.md`.

---

## Применимость для разработки оркестратора

### Сейчас: два пайплайна (→ один)

```
Пользовательский: Story → Architect → Tasks → Worker → .story/
Локальный:        /brainstorm → /triage → /plan → /implement → docs/plans/
```

Когда HITL заработает, локальный пайплайн — это просто CLI для HITL-роли:

```
/implement  →  fetch task from API  →  write .story/TASK.md  →  work  →  push  →  update API
```

Оркестратор зарегистрирован как project в собственной БД. Stories приходят через Telegram.
Человек (HITL) работает через те же `.story/` файлы, что и агент-воркер.
Скиллы `/brainstorm` и `/triage` остаются — они про discovery, не execution.

---

## Миграция: `.story/` vs прямые файлы

### Почему `.story/`, а не просто файлы в корне?

1. **Чистота** — story-файлы не коммитятся (добавить в .gitignore)
2. **Изоляция** — при смене стори можно очистить всю директорию
3. **Не конфликтует** с пользовательскими файлами
4. **Очевидно** что это managed by orchestrator

### Альтернатива: `/home/worker/.story/`

Вне workspace, не нужно .gitignore. Но менее удобно для Claude — `/workspace` это его CWD.

**Рекомендация**: `.story/` в workspace + `.gitignore` entry. Проще.

---

## Lifecycle `.story/` между сторями

### Нужно ли чистить?
**Да.** Каждая стори — отдельный контекст. Reports и context от предыдущей стори — мусор для новой.

### Как?
Engineering consumer при завершении стори:
1. Забрать все `.story/reports/*.md` → сохранить как story events
2. Удалить всю `.story/` директорию

Worker Manager при старте новой стори:
1. Убедиться что `.story/` пустая (или создать заново)

---

## Pipeline Convergence: HITL & Unified Pipeline

### Ключевой инсайт

«Саморазработка» — не третий режим. Оркестратор — просто ещё один пользовательский проект,
а его владелец — HITL этого проекта. Значит, режимов два:

1. **Пользовательский пайплайн** — оркестратор разрабатывает проекты (включая себя)
2. **HITL** — человек подключается к любому проекту в любой момент и продолжает работу

### Текущее состояние: два параллельных пайплайна

```
Пользовательский пайплайн:                  Локальная разработка (ad-hoc):
Story → Architect → Tasks → Worker            /brainstorm → /triage → /plan → /implement
Redis streams, TASK.md, INSTRUCTIONS.md       API tasks, CLAUDE.md, docs/plans/
Worker containers, isolated network           Local dev, make test-unit
CI gate (agent monitors GitHub)               Pre-push hooks (lint + tests)
```

Локальный пайплайн — костыль. Он существует потому, что пользовательский пайплайн
ещё не умеет HITL. Когда HITL заработает, локальный пайплайн станет частным случаем:
человек — просто ещё один «воркер», работающий через те же `.story/` файлы.

### Что нужно для конвергенции

#### A. Единая контекстная структура (`.story/`)

Работает одинаково для всех проектов и всех «воркеров» (агент или человек):

- `.story/TASK.md` — текущая задача (title + description + acceptance criteria + plan)
- `.story/STORY.md` — контекст стори (project description, env vars, story-level инструкции)
- `.story/CONTEXT.md` — предыдущая работа (compact summary: что сделано, коммиты, заметки)
- `.story/reports/` — отчёты завершённых тасок

Кто бы ни работал — агент в контейнере или человек за ноутбуком — контекст один и тот же.

При HITL-режиме `/implement` становится тонкой обёрткой:
1. Fetch task from API → write `.story/TASK.md`
2. Work (человек или Claude Code)
3. Push → update API

#### B. README как описание проекта

Сейчас у пользовательских проектов нет README — detailed_spec живёт только в БД.

Нужно:
- Scaffolder генерирует `README.md` из detailed_spec при создании проекта
- При feature-задачах README уже есть — worker читает его для контекста

#### C. Контекст для воркера — what's available, not force-fed

| Источник | Файл | Когда нужен |
|----------|------|-------------|
| Текущая задача | `.story/TASK.md` | Всегда (единственный файл, на который указывает `-p`) |
| Стори/проект | `.story/STORY.md` | При первом знакомстве с проектом |
| Предыдущая работа | `.story/CONTEXT.md` | Когда нужен контекст сиблингов |
| Отчёты предшественников | `.story/reports/` | При проблемах, debugging |
| Описание проекта | `README.md` | При первом знакомстве |
| Паттерны кода | `AGENTS.md` | При написании кода |
| Инструкции | `CLAUDE.md` | Автоматически (Claude Code native) |
| Прогресс | `PROGRESS.md` | При resume или retry |

Воркер сам решает, что ему нужно. `-p` указывает только на `.story/TASK.md`.

#### D. HITL: человек как воркер

Когда человек садится за проект:
1. `git pull` — получает `.story/` с текущей задачей
2. Читает `.story/TASK.md` — видит что делать
3. Читает `.story/CONTEXT.md` — видит что было сделано
4. Работает, коммитит, пушит
5. Обновляет `PROGRESS.md` или `REPORT.md`
6. Оркестратор подхватывает (через push webhook или polling)

Тот же flow, что и у агента. `.story/` — единый интерфейс для человека и машины.

Пример: я пишу "добавь фичу" → оркестратор декомпозирует → воркер начинает →
что-то не получается → я делаю `git pull` → вижу `.story/TASK.md` → доделываю →
пушу → оркестратор продолжает. Неважно, оркестратор это или todo_api — механизм один.

### Смерть локального пайплайна

Когда `.story/` и HITL заработают, локальные скиллы (`/plan`, `/implement`) станут
тонкими обёртками над тем же API:

```
/implement  →  fetch task from API  →  write .story/TASK.md  →  work  →  push  →  update API
```

Это не отдельный «локальный пайплайн», а CLI для HITL-роли.
Скиллы `/brainstorm` и `/triage` остаются — они про discovery, не про execution.

### Оркестратор как проект — никакой спец.логики

Для самозамыкания нужно только:

1. Зарегистрировать `codegen_orchestrator` в БД (project + repository)
2. Architect знает про monorepo (services/, shared/) — общая задача, не специфичная
3. Deploy action = self-update (watchtower / `docker compose up -d` / blue-green с migrate)

---

## CI Architecture: от agent-мониторинга к orchestrator-owned CI

### Текущая модель: агент мониторит CI

```
Worker pushes code
  → CI gate (_wait_for_ci_and_fix) запускается в engineering consumer
    → Поллит GitHub Actions каждые 15 сек
    → Timeout 10 мин на CI run, 60 мин общий
    → Если CI fails:
      → Классифицирует: infra vs code
      → Если code: спавнит нового developer worker для фикса
      → До 2 ретраев
    → Если CI passes → task DONE
```

**Проблемы**:
- Воркер ждёт CI 10-15 мин, ничего не делая. Контекст занят.
- CI-fix worker стоит дополнительных токенов (полная загрузка контекста заново).
- Architect создаёт отдельную CI task ("Run tests, verify CI green") — ещё один воркер, ещё больше токенов.
- Три уровня дупликации: каждый воркер пушит → CI → ждёт → фиксит. Плюс отдельная CI task в конце.

### Предлагаемая модель: локальные тесты + orchestrator CI

```
Worker runs local tests (make tests unit, make lint)
  → If tests fail → worker fixes locally (immediate feedback, zero wait)
  → If tests pass → worker commits & pushes
    → Task marked DONE by worker

Orchestrator (CI gate, отдельный процесс):
  → Monitors GitHub Actions асинхронно
  → If CI fails → creates new task "Fix CI failure: <error>" with logs
  → If CI passes → continues pipeline
```

**Что меняется**:

#### 1. Воркер запускает тесты локально

Инфраструктура уже есть: `orchestrator dev-env start-infra db redis` + `make tests unit`.

Workflow воркера:
```
1. Implement changes
2. Run `make lint` → fix if fails
3. Run `make tests unit` → fix if fails
4. Run integration tests if applicable
5. Commit & push
6. Write REPORT.md
7. Done.
```

Воркер не ждёт CI. Он проверяет качество локально и уходит. Быстрая обратная связь вместо 15-минутного ожидания.

#### 2. Smoke test — воркер поднимает стек

Для полной уверенности воркер может поднять приложение и проверить его:

```bash
# Start the application stack with test/mock tokens
orchestrator dev-env start-infra db redis
make run-backend  # or docker compose up backend
curl http://localhost:8000/health  # smoke check
```

Для tg_bot: fake token (бот не подключится к Telegram, но код инициализации проверится).
Для backend: полный smoke — API endpoints отвечают, DB работает.

Это полезнее CI, потому что воркер видит реальные ошибки сразу. И может починить.

#### 3. Оркестратор владеет CI gate

CI gate выносится из engineering consumer в отдельный механизм:

```
GitHub webhook (ci.yml completed)
  → API получает статус
  → Если success: ничего не делать (pipeline продолжается)
  → Если failure:
    → Скачать CI logs
    → Классифицировать: infra vs code
    → Если infra: rerun failed jobs / alert admin
    → Если code: создать новую задачу "Fix CI: <error>"
      → Эта задача — обычная, встаёт в очередь
      → Worker получает её с CI logs в .story/TASK.md
```

#### 4. CI task архитектора исчезает

"Run tests, verify CI green" больше не нужна как отдельная задача. Каждый воркер сам запускает тесты. CI мониторинг — ответственность оркестратора.

Architect создаёт только бизнес-задачи. Тестирование — часть каждой задачи (как и сейчас, но без отдельного CI task).

#### 5. Гранулярность задач: одна доработка = одна задача

Сейчас architect дробит мелко, а вся стори делается в одной сессии через `--resume`.
Тесты и пуш — только в конце, отдельной CI задачей. Фактически таски не изолированы.

Если каждая задача завершается локальными тестами и пушем, то:
- Каждая задача — self-contained единица с валидацией
- Но чтобы не раздувать время и не жечь GitHub Actions минуты,
  **одна задача = одна небольшая, атомарная доработка**
- Architect должен дробить по-другому: не "create models", "create endpoints", "create tests"
  а "implement weather API endpoint with tests" — одна цельная фича за одну задачу
- CI task исчезает — валидация встроена в каждую задачу

### Trade-offs новой модели

**(+) Экономия токенов**: Нет CI-wait idle time, нет отдельных CI-fix workers, нет CI task.
**(+) Быстрая обратная связь**: Воркер видит test failures сразу, не через 15 мин.
**(+) Проще pipeline**: Нет `_ci_gate.py` с его 500 строками, нет classification, нет respawn.
**(+) CI failures — обычные задачи**: Единообразная обработка, тот же механизм retry.

**(-) CI может поймать то, что локальные тесты пропустят**: Docker build, dependency resolution, platform-specific issues. Но это редко и лечится создованием задачи.
**(-) Нужна инфраструктура для локальных тестов**: Уже есть (`orchestrator dev-env`), но не для всех типов проектов.
**(-) Smoke test в контейнере**: Нужно уметь поднимать стек. Для простых backend — ок. Для multi-service — сложнее.

### Переходный период

Не обязательно всё менять сразу:

**Phase 1**: Добавить `make tests unit` в workflow воркера (INSTRUCTIONS.md). CI gate остаётся.
**Phase 2**: Убрать CI task из architect. CI gate мониторит после последнего push в стори.
**Phase 3**: Вынести CI gate в webhook-based механизм. Воркер полностью перестаёт ждать CI.
**Phase 4**: Добавить smoke test через `orchestrator dev-env`.

---

## Convergence Summary

```
                    Unified Pipeline
                    ═══════════════

         Agent (in container)           Human (HITL)
         ───────────────────           ─────────────
         PO/Telegram → Story           (same story)
         Architect → Tasks             (same tasks)
         Worker → .story/              git pull → .story/
           TASK.md                       TASK.md
           STORY.md                      STORY.md
           CONTEXT.md                    CONTEXT.md
         Local tests → push            Local tests → push
         CI (async, orchestrator)       CI (async, orchestrator)
         Deploy (per project type)     Deploy (per project type)
```

Оркестратор — просто ещё один project в БД. Deploy action per project type:
- User projects: DevOps subgraph → server
- Orchestrator: watchtower / `docker compose up -d`

---

## Action Items

- → new task: "File-first worker context: replace `-p` prompt with `.story/` file structure"
  Включает: создание `.story/TASK.md`, `.story/STORY.md`, `.story/CONTEXT.md`;
  минимизация `-p` до одной строки; обновление developer.py, worker_spawner.py, wrapper.py

- → new task: "Merge AUDIT_REPORT.md into REPORT.md, remove duplicate"
  Audit — это секции Issues+Suggestions в REPORT.md. Убрать AUDIT_REPORT.md
  из e2e-run skill. Обновить INSTRUCTIONS.md если нужно усилить секцию Issues.

- → new task: "Story-level context via `.story/STORY.md`"
  Записывать project context, modules, env hints, story-level инструкции
  в `.story/STORY.md`. Доступен всем воркерам в стори через файловую систему.

- → new task: "Hybrid --resume strategy: fresh session on first task and retries"
  Реализовать логику: свежая сессия для первой таски в стори + при retry,
  `--resume` для последующих тасок.

- → idea: "HITL CLI: `/implement` as thin wrapper over `.story/` + API"
  `/implement` → fetch task from API → write `.story/TASK.md` → work → push → update API.
  Не отдельный пайплайн, а CLI для HITL-роли в unified pipeline.

- → idea: "Compact CONTEXT.md generator"
  Вместо `_build_story_context()` с events и lifecycle —
  генерировать 5-10 строк на таску: что сделано, коммит, важные заметки.

- → new task: "Persist worker REPORT.md in `.story/reports/` instead of deleting"
  Сейчас wrapper удаляет REPORT.md после чтения. Единственная копия — task_event в БД.
  При клинапе DB записи теряются. Нужно: сохранять в `.story/reports/task-{id}.md`,
  чтобы (a) следующие воркеры видели отчёты предшественников, (b) данные персистили вне БД.

- → new task: "E2E skill: save worker reports to docs/ BEFORE cleanup"
  Step 7 в скилле описывает сбор, но в weather_bot прогоне отчёты не были
  сохранены в файлы до DELETE FROM task_events. Нужен обязательный шаг.

- → new task: "Worker local tests: add `make tests unit` + `make lint` to INSTRUCTIONS.md"
  Воркер должен запускать тесты локально перед push. Быстрая обратная связь
  вместо 15-мин ожидания CI. Phase 1 перехода к orchestrator-owned CI.

- → idea: "Architect: atomic task granularity (one feature = one task)"
  Вместо "create models" + "create endpoints" + "create tests" →
  "implement weather API endpoint with tests". Каждая задача — одна цельная
  доработка с тестами. Совместно с отказом от CI task и локальными тестами.

- → new task: "Remove CI task from architect, move CI gate to webhook-based"
  Architect не создаёт "Run tests, verify CI green" задачу.
  CI gate из engineering consumer → webhook handler в API.
  CI failure → новая задача "Fix CI: <error>". Phase 2-3.

- → idea: "Scaffolder generates README.md from detailed_spec"
  User projects не имеют README. Воркеру нужно описание проекта.
  Scaffolder может генерировать README.md из project.config.detailed_spec.

- → new task: "Register orchestrator as project in DB"
  Просто зарегистрировать codegen_orchestrator как project + repository в БД.
  Никакой спец.логики — обычный проект. Deploy action = self-update (watchtower).

- → idea: "Worker smoke test via `orchestrator dev-env`"
  Воркер поднимает стек через orchestrator CLI и делает smoke check.
  Для backend: curl /health. Для tg_bot: fake token + init check.
  Phase 4 — после стабилизации локальных тестов.
