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

Те же принципы работают для наших собственных тасок:

### Сейчас: backlog.md, plans, CLAUDE.md — рассинхрон

```
docs/backlog.md (auto-generated из DB)
docs/DEV_PIPELINE.md (ручной, часто stale)
CLAUDE.md (ручной, длинный)
API (source of truth для тасок)
```

Проблема: Claude должен делать curl к API чтобы узнать статус таски, потом читать файлы для контекста, потом ещё куда-то за планом.

### Предложение: `.task/` директория для orchestrator dev

По аналогии с `.story/`:

```
.task/
├── CURRENT.md          # Текущая задача (симлинк или генерируется из API)
├── PLAN.md             # План реализации (из API, но файл)
├── CONTEXT.md          # Связанные таски, зависимости
└── CHANGELOG_DRAFT.md  # Черновик для CHANGELOG (собирается при коммите)
```

`/implement` мог бы:
1. Получить таску из API
2. Записать CURRENT.md и PLAN.md в `.task/`
3. Работать с файлами как обычно
4. По завершении — обновить API из файлов

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

- → idea: "`.task/` directory for orchestrator dev workflow"
  Применить file-first подход к нашему собственному dev pipeline.
  `/implement` пишет `.task/CURRENT.md` и `.task/PLAN.md` из API.

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
