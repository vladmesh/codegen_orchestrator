---
id: bs-4234f04e
status: triaged
title: "Dev Pipeline 2 — Artifact Cascade"
created_at: 2026-03-07T12:37:04.595777Z
---

# Dev Pipeline: Каскад артефактов и скиллы

> **Дата**: 2026-03-03
> **Контекст**: Продолжение dev-pipeline-1A и 1B. Фокус на пайплайне разработки как замкнутой системе, максимальная автоматизация через фиксированный набор Claude skills.
> **Status**: triaged

---

## 1. Два контура системы

### Контур 1: Data flow (артефакты)
Документы = "состояние" проекта. Поддерживаются автоматически скиллами.

### Контур 2: Command flow (скиллы)
Набор команд, каждая читает и пишет артефакты. Система замкнута — каждый скилл обновляет артефакты, которые читают другие скиллы.

---

## 2. Каскад артефактов (сверху вниз)

Каждый уровень — **спецификация** для уровня ниже и **тест** для уровня выше. Фрактальный TDD.

```
ROADMAP              ← человек
  ↓ декомпозиция (человек + агент)
USER STORIES         ← docs/USER_STORIES.md
  ↓ формализация acceptance criteria
E2E TESTS            ← .claude/skills/e2e-* + скрипты
  ↓ "что нужно чтобы это прошло?"
BACKLOG              ← docs/backlog.md
  ↓ декомпозиция (через brainstorm если сложно)
PLAN                 ← docs/plans/<task>.md
  ↓ каждый шаг плана = спецификация
UNIT TESTS           ← tests/unit/
  ↓ реализация
CODE                 ← services/
```

### Где интеграционные тесты

Появляются на уровне **плана**, из **стыков между шагами**:

```
Plan: "Worker Network Isolation"
  Step 1: Создать отдельную docker network     → unit test
  Step 2: Настроить worker-manager              → unit test
  Step 3: Worker пишет в свою DB, не в основную → unit test
  ─────────────────────────────────────────────
  Стык 1-2-3: worker реально изолирован        → INTEGRATION test
```

- **Unit** = "шаг плана работает изолированно"
- **Integration** = "шаги плана работают вместе"
- **E2E** = "user story выполняется от начала до конца"

### Два пути от E2E к Backlog

**Путь A (forward planning):** User Story → E2E скрипт (красный) → "что нужно?" → Backlog tasks

**Путь B (feedback loop):** E2E report (красный) → /triage → Backlog tasks (баг-фиксы)

### Инфра — пресет, не часть каскада

CI, pre-push/pre-commit hooks, linters, Docker/compose, test framework — задаётся пользователем до начала проекта через service-template. Это **среда исполнения** каскада.

---

## 3. Граница автоматизации

```
ЧЕЛОВЕК (с помощью агента)          АГЕНТ (автономно)
─────────────────────────────────────────────────────
ROADMAP
USER STORIES
E2E TESTS (написание)
          ─ ─ ─ ─ ─ ─ граница ─ ─ ─ ─ ─ ─ ─
                                    E2E TESTS (запуск)
                                    TRIAGE → BACKLOG
                                    PLAN
                                    UNIT/INTEGRATION TESTS
                                    CODE
                                    CHANGELOG/STATUS
```

Граница со временем ползёт вверх. Сегодня человек пишет E2E, завтра — только user stories, послезавтра — только roadmap.

**Промежуточный шаг**: Сократовский скилл для E2E — агент задаёт правильные вопросы, человек отвечает, агент формализует в тест. Не убирает человека из цикла, но снижает когнитивную нагрузку.

---

## 4. Граф артефактов (полная картина с потоками)

```
                    ROADMAP
                       │
                       ↓ (декомпозиция)
                 USER STORIES
                    │      ↑
        (формализация)     (валидация: E2E зелёный = story done)
                    ↓      │
                  E2E TESTS ←──── E2E REPORTS (feedback)
                    │                   │
                    ↓                   ↓
                 BACKLOG  ←────────  /triage
                    │
                    ↓ (декомпозиция)
              PLAN + BRAINSTORM
              │              │
              ↓              ↓
         UNIT TESTS    INTEGRATION TESTS
              │              │
              ↓              ↓
             CODE ──────→ CI (pre-push, lint)
                              │
                              ↓
                         E2E REPORTS (→ feedback loop)
```

---

## 5. Форматы артефактов

```
docs/
├── STATUS.md          # Указатель: текущий backlog item + plan step
├── ROADMAP.md         # Вехи (3-5), каждая = набор backlog items
├── CHANGELOG.md       # Факты: что сделано, по датам
├── backlog.md         # Очередь работы + Ideas (приоритизированная)
├── USER_STORIES.md    # User stories со ссылками на E2E и backlog items
├── plans/             # Детальные планы для сложных задач
├── brainstorms/       # Развёрнутые обсуждения (input для backlog)
└── e2e_results/       # Отчёты E2E (input для backlog)
```

**Принцип: один основной писатель на артефакт** — предотвращает конфликты.

---

### 5.1 USER_STORIES.md

Формат уже хороший. Добавить **явные связи** с E2E и backlog для замыкания каскада:

```markdown
### US1: Свой токен Telegram бота
**Приоритет**: Критический
**Статус**: В разработке (~80%)

**Как** пользователь
**Хочу** использовать свой Telegram Bot Token
**Чтобы** бот имел моё имя и был под моим контролем

**Acceptance Criteria**:
- [ ] Система принимает токен от пользователя
- [ ] Токен сохраняется как секрет проекта
- [ ] Деплой использует пользовательский токен

**E2E**: `.claude/skills/e2e-run` (Level C, todo_api scenario)
**Backlog Items**: #22, #21
```

Без связи E2E↔Story агент не может автоматически проверить "user story выполнена?".

---

### 5.2 backlog.md

**Решение**: один файл, три секции — Queue, Ideas, Done.

- **Queue** — упорядочена, первый элемент = следующая задача. Поля фиксированы для парсинга скиллами.
- **Ideas** — однострочники с источником. Как только идея требует больше одной строки → `/brainstorm` → `brainstorms/`.
- **Done** — сжатый список (id + название + дата). Детали в CHANGELOG. Последние 10 записей, старые удаляются.

**Жизненный цикл идей:**
- **Откуда**: `/brainstorm` (побочная мысль), `/triage` (паттерн "было бы неплохо"), `/audit` (возможность, не проблема), `/implement` (заметил рядом, не отвлекаюсь).
- **Куда**: при `/checkpoint` каждая идея → промоутить в Queue с приоритетом | развернуть через `/brainstorm` | удалить как неактуальную.

```markdown
# Backlog

## Queue (ordered by priority, first = next)

### #22 Worker Network Isolation
- **Priority**: HIGH
- **User Story**: —
- **Plan**: docs/plans/worker-network-isolation.md
- **Status**: in-progress (step 3/4)
- **Brief**: Отдельная сеть codegen_worker, удаление workaround

### #2 Agent Hierarchy & Incident Response
- **Priority**: HIGH
- **User Story**: —
- **Plan**: —
- **Status**: pending
- **Brief**: TaskAssessor, Watchdog, shared session memory

### #8 Workspace Failure Counter
- **Priority**: MEDIUM
- **User Story**: —
- **Plan**: —
- **Status**: pending
- **Brief**: Wipe workspace после 2 падений, отклонение после 3

## Ideas
- Self-hosted GitLab или GH runner на VPS (источник: E2E failure rate, 2026-03-02)
- Docker Python SDK для worker-manager (источник: audit-v2)
- "Добавить батарейку" к существующему проекту (источник: US3)

## Done (last 10)
- #15 Resolve Enum Divergence — 2026-02-28
- #16 Consolidate ServiceModule — 2026-02-27
```

**Ключевые отличия от текущего формата:**
- Queue упорядочена — первый элемент = следующая задача
- Поля фиксированы — скилл может парсить
- Done сжат — только id + название + дата, детали в CHANGELOG
- Зачёркнутая история подзадач убрана — это артефакт выполнения, место ей в CHANGELOG
- Ideas — однострочники, не развёрнутые описания

---

### 5.3 STATUS.md — указатель, не документ

```markdown
# STATUS

## Current Task
- **Backlog**: #22 Worker Network Isolation
- **Plan**: docs/plans/worker-network-isolation.md
- **Step**: 3/4 — Тесты и валидация
- **Done Steps**:
  - 1/4 Создание сети codegen_worker, dual-homing
  - 2/4 Удаление workaround (project-db, _patch_db_hostname)

## Blocked
(пусто или список блокеров)

## Last Checkpoint
- **Date**: 2026-03-03
- **E2E**: Level C pass (todo_api)
```

Агент при старте сессии получает полный контекст за O(1).

---

### 5.4 CHANGELOG.md

Формат [Keep a Changelog](https://keepachangelog.com/), группировка по датам. Заполняется скиллом `/implement` после каждого коммита — человекочитаемая выжимка из git log.

```markdown
# Changelog

## 2026-03-03
### Fixed
- CI gate: filter by commit SHA to prevent scaffold CI satisfying implementation gate
### Added
- Worker network isolation plan (#22)

## 2026-03-02
### Fixed
- Remove obsolete EXEC_MODE=native references
### Changed
- E2E report: todo_api Level C — deploy failed, makemigrations investigated
```

Секции: `Added`, `Changed`, `Fixed`, `Removed`.

---

### 5.5 ROADMAP.md

Вехи = группировки backlog items. Каждый `[ ]` ссылается на backlog `#id`.

```markdown
# Roadmap

## Stable Pipeline (scaffold -> code -> CI -> deploy)
- [x] Redis Streams unification (#3+#5)
- [x] Worker reuse for CI fix loop (#9)
- [ ] Worker network isolation (#22)
- [ ] Fix & consolidate test suites (#6)

## Reliability & Self-Recovery
- [ ] Agent hierarchy & incident response (#2)
- [ ] Workspace failure counter (#8)
- [ ] Deploy pre-check (#21)

## Dev Process Automation
- [ ] Dev pipeline skills (implement, audit, triage, checkpoint)
- [ ] Self-maintaining docs (CHANGELOG, STATUS auto-update)
```

---

### 5.6 Plans — стандартизированный формат

Каждый шаг = **Input/Output/Test**. Из этого `/implement` генерирует unit/integration тесты.

```markdown
# Plan: Worker Network Isolation (#22)

## Context
(почему, ссылки на brainstorms)

## Steps
1. [ ] Создать сеть codegen_worker, dual-homing bridge-сервисов
   - **Input**: docker-compose.yml
   - **Output**: новая сеть, redis/api/worker-manager на обеих сетях
   - **Test**: unit — сеть создаётся в compose config

2. [ ] Удаление workaround
   - **Input**: manager.py, compose templates
   - **Output**: project-db alias и _patch_db_hostname() удалены
   - **Test**: unit — worker config не содержит project-db

3. [ ] Тесты и валидация
   - **Input**: —
   - **Output**: E2E Level C pass
   - **Test**: integration — worker не видит postgres оркестратора

4. [ ] Cleanup документации
   - **Input**: docs/
   - **Output**: обновлённые docs
   - **Test**: —
```

Интеграционные тесты появляются из **стыков между шагами** (подробнее — секция 2).

---

### 5.7 Brainstorms

Развёрнутые обсуждения. Формат свободный, но с обязательным header и lifecycle-статусом:

```markdown
# Brainstorm: <тема>

> **Дата**: ...
> **Контекст**: ...
> **Status**: draft | done | triaged

(содержание)

## Action Items
- → backlog #XX (если задача создана)
- → idea: "..." (если не дозрела до задачи)
```

**Lifecycle:**
- `draft` — обсуждение в процессе, `/triage` не трогает
- `done` — автор подвёл итог, Action Items заполнены, готов к триажу
- `triaged` — `/triage` обработал, Action Items раскиданы в backlog

`/brainstorm` создаёт файл со `Status: draft`, при завершении ставит `Status: done`.
`/triage` читает только `done`, после обработки ставит `Status: triaged`.

Секция `Action Items` — мост к backlog. Без неё брейншторм "повисает" без последствий.

---

### 5.8 E2E Reports

Формат генерируется скиллом `/e2e-run`. Основная часть (Timeline, Engineering Results, Deployment Verification) — свободная, это контекст для человека. Стандартизируется только секция **Problems Found** — для машинного парсинга скиллом `/triage`.

```markdown
## Problems Found

### P1: Deploy timeout on first attempt
- **Severity**: major
- **Type**: orchestrator
- **Backlog**: new
- **Description**: deploy.yml timed out at 5min, retry succeeded

### P2: POSTGRES_HOST mismatch in .env
- **Severity**: minor
- **Type**: template
- **Backlog**: — (known issue)
- **Description**: `.env` has `POSTGRES_HOST=project-db` but workers need `db`
```

При clean pass:
```markdown
## Problems Found
None.
```

**Поля для `/triage`:**
- **Severity** (critical / major / minor / info) — определяет приоритет в backlog
- **Type** — определяет маршрутизацию (см. ниже)
- **Backlog** — ссылка `#XX` на существующую задачу, `new` если нужно создать, `—` если не нужно

**Маршрутизация по Type (кросс-проектная):**

| Type | Куда | Backlog |
|------|------|---------|
| `orchestrator` | `docs/backlog.md` (этот проект) | `/triage` создаёт задачу |
| `template` | service-template (другой проект) | `/triage` добавляет задачу в `/home/vlad/projects/service-template/docs/backlog.md` (свободный формат пока) и коммитит. Формат бэклога service-template стандартизируем позже |
| `meta` | правка самого e2e скилла | `/triage` создаёт задачу в `docs/backlog.md` с пометкой `[meta]` |
| `infra` | prod_infra / ansible | `/triage` выводит, человек решает |

На текущем этапе `/triage` автоматически пишет задачи для `type: orchestrator` (в наш backlog) и `type: template` (в backlog service-template, свободный формат + коммит). Для `meta` и `infra` — формирует список для ручного решения.

---

### 5.9 Завершённые планы

Планы в `docs/plans/` удаляются при завершении задачи. Суть фиксируется в CHANGELOG (что сделано) и в Done-секции backlog (id + название + дата). Сам план — рабочий документ, после выполнения не несёт ценности.

Скилл `/implement` при закрытии задачи: удаляет `docs/plans/<task>.md`, обновляет STATUS.md (убирает ссылку на план).

---

## 6. Скиллы

### 6.1 Полный список (3 существующих + 5 новых)

**Существующие (E2E):**

| Скилл | Что делает | Артефакт |
|-------|-----------|----------|
| `/e2e-run` | Запуск E2E теста | `e2e_results/<report>.md` |
| `/e2e-check` | Проверка результатов | — (вывод в консоль) |
| `/e2e-cleanup` | Очистка после E2E | — |

**Новые (Dev Pipeline):**

| Скилл | Читает | Делает | Пишет |
|-------|--------|--------|-------|
| `/brainstorm` | код, доки, контекст | Думает, структурирует | `brainstorms/<topic>.md` (Status: draft → done) |
| `/plan` | `backlog.md`, `brainstorms/`, код | Декомпозирует задачу на шаги с Input/Output/Test | `plans/<task>.md`, `STATUS.md` |
| `/implement` | `STATUS.md` → `plans/` → код | TDD цикл, коммит | код, `CHANGELOG.md`, `backlog.md` (→Done), `STATUS.md`, удаляет план |
| `/triage` | `e2e_results/`, brainstorms (Status: done), audit reports | Классифицирует, маршрутизирует | `backlog.md`, service-template backlog (коммит), brainstorms (→triaged) |
| `/next` | `backlog.md` Queue | Выбирает top-1, линкует план если есть | `STATUS.md` |
| `/audit` | весь код | Ищет dead code, smells, security | items в `backlog.md` |
| `/checkpoint` | всё | audit + triage + обновление доков + рекомендация следующей задачи | `CHANGELOG.md`, `ROADMAP.md`, `STATUS.md` |

### 6.2 Зоны ответственности и границы

**`/brainstorm`** — единственный скилл для "думания". Не создаёт задачи напрямую, только Action Items для последующего `/triage`. Ставит `Status: done` при завершении.

**`/plan`** — декомпозиция одной задачи. Не выбирает задачу (это `/next`), не реализует (это `/implement`). Создаёт план со стандартизированными шагами (Input/Output/Test). Обновляет STATUS.md.

**`/implement`** — основной рабочий скилл. Без аргументов берёт текущую задачу из STATUS.md. С аргументом (`/implement #8`) — переключается на указанную (покрывает hotfix flow). При завершении: обновляет CHANGELOG, переносит задачу в Done, удаляет план, очищает STATUS.md. Не выбирает следующую задачу.

**`/triage`** — обработка всех "входящих". Три источника: e2e_results (по Problems Found), brainstorms (Status: done, по Action Items), audit reports. Маршрутизирует по Type: orchestrator → наш backlog, template → service-template backlog (коммит), meta → наш backlog [meta], infra → список для человека.

**`/next`** — маленький скилл-переключатель. Читает Queue, ставит top-1 в STATUS.md. Точка где человек может вмешаться ("нет, возьми #8"). Если у задачи есть план — линкует, если нет — сигнализирует что нужен `/plan`.

**`/audit`** — сканирование кодовой базы. Результат → задачи в backlog напрямую (не через `/triage`, т.к. аудит уже классифицирован). Перезаписывает `docs/audit.md`.

**`/checkpoint`** — мета-скилл. Вызывает `/audit` (если давно не было) и `/triage`. Обновляет CHANGELOG, ROADMAP, STATUS. Выводит саммари: что сделано с прошлого чекпоинта, блокеры, рекомендация следующей задачи. Периодичность: каждые 5-7 задач или по запросу.

### 6.3 Замкнутый цикл

```
/checkpoint ──→ /audit ──→ backlog
     │          /triage ──→ backlog
     │                        │
     │                        ↓
     │                   /next → STATUS.md
     │                        │
     │                   /plan (если нетривиальная)
     │                        │
     └──────────────→ /implement ──→ CHANGELOG
                          │
                          ↓
                     /e2e-run ──→ e2e_results/
                          │
                          ↓
                     /triage ──→ backlog (новые баги)
                          │
                          ↓
                     /next (следующая задача) ...
```

### 6.4 Что НЕ является отдельным скиллом (и почему)

- **Hotfix** — покрывается `/implement #XX` с явным аргументом
- **E2E design** (Сократовский диалог) — пока рано, граница автоматизации ещё не дошла
- **Кросс-проектный фикс** — за scope, другой проект = отдельная сессия
- **Update docs** — не отдельный скилл, а часть `/implement` и `/checkpoint`

### 6.5 Принципы проектирования скиллов

1. **Идемпотентность**: повторный вызов не ломает артефакты
2. **Один писатель**: у каждого артефакта "основной" скилл-писатель
3. **Минимальный ввод**: без аргументов берёт из STATUS.md, аргументы — для override
4. **Артефакт = output**: каждый скилл оставляет след в файлах
5. **Машиночитаемость**: backlog items с `#id`, plans со step numbers, changelog с датами

---

## 7. Точки, где сейчас нужен человек

| Точка | Почему | Как убрать (в будущем) |
|-------|--------|------------------------|
| Выбор задачи | Контекст, приоритеты | Строгая приоритизация: P0 > P1 > P2, `/implement` берёт top-1 |
| Согласование плана | Изменения contracts/schema | Маркер `needs-approval` в backlog, остальные — автоматически |
| "Достаточно ли хорошо" | Субъективная оценка | E2E + unit tests как объективный критерий |
| Приоритизация | Стратегические решения | Человек задаёт вехи в ROADMAP, автоматика внутри вехи |
| Написание E2E | Требует понимания UX | Сократовский скилл как промежуточный шаг |

---

## 8. План перехода (от текущего состояния к целевому)

### Фаза 0: Создание новых артефактов (пока старые файлы ещё есть)

Старые plans, investigations, brainstorms — **источник для backfill**. Создаём новые артефакты, опираясь на них.

1. **`docs/CHANGELOG.md`** — backfill из git log + старых plans/investigations (там история решений) за последние 2-3 недели
2. **`docs/ROADMAP.md`** — собрать вехи из текущего backlog + обсуждения (Stable Pipeline, Reliability, Dev Process, MVP phases из 1B)

### Фаза 1: Cleanup (старые файлы больше не нужны)

**Удалить:**
- `docs/investigations/` — все 11 файлов (логи починки багов, суть уже в CHANGELOG)
- `docs/plans/` выполненные: `po-react-agent.md`, `redis-streams-unification.md`, `deploy-architecture.md`, `worker-reuse-ci-fix.md`
- `docs/brainstorms/` выполненные/отменённые: `service-template-and-dev-environment.md`, `ci-pipeline-redesign.md`, `deploy-architecture.md`, `integration-test-speedup.md`

**Консолидировать перед удалением:**
- Актуальные brainstorms (`agent-hierarchy.md`, `worker-db-isolation.md`, `worker-workspace-persistence.md`) — проверить что суть есть в backlog, если да — удалить
- Актуальные plans (`worker-network-isolation.md`, `workspace-persistence.md`) — оставить если задача активная, иначе суть в backlog и удалить

**Поправить:**
- `README.md` — убрать `prod_infra`, `preparer` → `Scaffolder`
- `AGENTS.md` — убрать `make test-all`, добавить ссылки на skills, упростить до высокоуровневого overview

### Фаза 2: Переформатирование артефактов

Приводим существующие файлы к целевым форматам (секция 5).

1. **`docs/backlog.md`** — переформатировать в Queue/Ideas/Done:
   - Queue: упорядочить задачи, фиксированные поля (Priority, User Story, Plan, Status, Brief)
   - Ideas: вынести из LOW и Ideas в однострочники с источником
   - Done: сжать до последних 10 (id + название + дата), детали уже в CHANGELOG
   - Убрать зачёркнутую историю подзадач
2. **`docs/STATUS.md`** — формат указателя (Backlog #, Plan, Step, Done Steps, Blocked, Last Checkpoint)
3. **`docs/USER_STORIES.md`** — добавить поля `E2E` и `Backlog Items` к каждой story
4. **Оставшиеся `brainstorms/*.md`** — добавить `> **Status**: done | draft` в header

### Фаза 3: Скиллы (порядок по частоте использования)

Артефакты готовы → можно писать скиллы, которые их читают/пишут.

1. **`/next`** — маленький, нужен сразу (кто ставит задачу в STATUS)
2. **`/implement`** — основной рабочий скилл, можно сразу начать использовать
3. **`/triage`** — замыкает петлю e2e → backlog
4. **`/plan`** — нужен когда дойдём до нетривиальной задачи
5. **`/brainstorm`** — формализация процесса обсуждения
6. **`/checkpoint`** — мета-скилл, вызывает остальные
7. **`/audit`** — по необходимости

### Зависимости между фазами

```
Фаза 0 (CHANGELOG, ROADMAP)
    │ используют старые файлы как источник
    ↓
Фаза 1 (Cleanup)
    │ чистая документация
    ↓
Фаза 2 (Переформатирование)
    │ артефакты в целевом формате
    ↓
Фаза 3 (Скиллы)
    │ скиллы читают/пишут артефакты
    ↓
Рабочий цикл: /next → /plan → /implement → /e2e-run → /triage → /next ...
```

---

## Подумать позже

### Из 1A: Процесс
- **Kanban + чекпоинты** — чекпоинт каждые 5-7 задач или раз в неделю
- **Глубина скиллов** — `e2e-run` = 825 строк (детерминированный). Для `/implement` такая детализация контрпродуктивна
- **Мета-перенос** — всё что отработано руками → кандидат на автоматизацию в оркестраторе (changelog → агент, триаж → parser, аудит → Watchdog, checkpoint → scheduler)

### Из 1B: MVP
- **MVP Roadmap phases**: Phase 1 (Foundation) → Phase 2 (Quality, MVP cutoff) → Phase 3 (Capabilities) → Phase 4 (Scale)
- **Ключевые решения**: Admin UI = must have для MVP, Assessor node вместо Architect для MVP, Frontend battery = post-MVP