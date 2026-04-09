# Brainstorm: Sprint-based development pipeline for orchestrator

> **Дата**: 2026-04-09
> **Контекст**: Переход от плоского backlog к sprint-based workflow с встроенным управлением техдолгом
> **Status**: triaged

---

## Current State

Текущий dev pipeline — плоский backlog (`docs/backlog.md`) + набор скиллов:
- `/brainstorm` → `/triage` → `/plan` → `/implement` → `/checkpoint`
- Человек вручную выбирает какой скилл запустить и над какой задачей
- Нет группировки задач (47 в очереди — от CRITICAL security до LOW "Rust PoC")
- Нет формального definition of done для группы задач
- Нет принудительных гейтов (audit/e2e можно забыть)
- `/checkpoint` существует, но вызывается "каждые 5-7 задач" — на практике когда вспомнишь

**Что работает хорошо:**
- Skill feedback loop (`/optimize`) — скиллы улучшаются сами
- Shared recipes (`pipeline-recipes.md`) — единое место для bash-команд
- `/brainstorm` как adversarial discussion — хорошо для архитектурных решений
- `/escort` и `/e2e-run` — мониторинг продакшн-пайплайна
- TDD в `/implement` — Red→Green→Refactor дисциплина

**Что не работает:**
- Когнитивная нагрузка на человека: "что делать дальше?" каждый раз
- Техдолг откладывается ("потом починим noqa", "потом split github.py")
- Нет ритма: иногда 10 задач без audit, иногда audit два раза подряд
- ROADMAP — auto-generated из DB, не управляет приоритетами напрямую

---

## Problem / Opportunity

Нужна структура которая:
1. Снижает когнитивную нагрузку (человек говорит `/go`, система решает что делать)
2. Встраивает погашение техдолга в ритм (нельзя забыть audit, нельзя пропустить refactor)
3. Группирует связанные задачи (sprint = фокус на одной теме)
4. Имеет формальные гейты между этапами (phase не закрыта → дальше нельзя)
5. Масштабируется на сложность оркестратора (12+ сервисов, live pipeline тесты)

---

## Решение: DnD-модель адаптированная под оркестратор

Полный порт sprint/phase/task иерархии из DnD Simulator, адаптированный под специфику оркестратора (multi-service, live pipeline, тяжёлые e2e).

### Иерархия

```
Sprint NNN-slug/
├── sprint.md              — цель, фазы, решения, deferred
├── tasks/
│   ├── phase0-task1-*.md  — задача (описание, тесты, acceptance criteria, status)
│   ├── phase1-task1-*.md
│   └── ...
└── e2e/
    └── sprint-report.md   — финальный e2e отчёт
```

- `docs/STATUS.md` — текущее состояние (спринт, фаза, прогресс). Единственный файл который `/go` читает для решений.
- `docs/backlog.md` — **deferred pool**: техдолг, мелкие баги, идеи на будущее. НЕ рабочая очередь. Разгребается в техспринтах.
- `docs/sprints/NNN-slug/` — рабочая директория спринта. Задачи = файлы.
- `docs/VISION.md` — архитектурные инварианты. Audit проверяет отклонения.

### Роль backlog.md

Backlog — это **не очередь задач**, а **отложенный пул**:
- Всё что не влезает в текущий спринт → backlog
- Audit/e2e находки не связанные с текущим кодом → backlog
- Мелкие пожелания, идеи → backlog
- Каждый 5-й спринт = техспринт, формируется из backlog
- Если backlog распух (>30 items) — техспринт раньше

Текущие 47 задач из queue → все уходят в backlog. Первый спринт = техспринт для разгрузки и обкатки новой системы.

### Тестовая стратегия

**Unit tests** — пишутся в рамках задач (TDD: Red→Green→Refactor в `/implement`).

**Integration tests** — пишутся и обновляются при закрытии фазы (`/close-phase`):
1. Прогнать существующие integration tests
2. Проверить: есть ли новые code paths без integration coverage?
3. Написать недостающие integration tests
4. Обновить протухшие тесты (изменился API контракт и т.д.)
5. Если новый integration test упал → создать доп. задачу в этой же фазе → починить → снова `/close-phase`

Это решает проблему протухания тестов: они актуализируются каждую фазу, а не "когда вспомнишь".

**E2E** (`/e2e-run`) — только в конце спринта. Полный pipeline test, уровень глубины на усмотрение агента (чем больше затронули — тем полнее тест).

### Завершение спринта (Sprint Endgame)

После закрытия последней feature-фазы запускается обязательная финальная последовательность:

```
Все feature-фазы COMPLETE
        ↓
   ┌────┴────┐
   │  audit  │  — сканирование кода
   │  e2e    │  — pipeline test (глубина по масштабу изменений)
   └────┬────┘
        ↓
   Общая fix-фаза
   (quick-fixes из audit + баги из e2e +
    рефакторинг напрямую связанный с изменённым кодом)
        ↓
   Что не влезло → backlog
        ↓
   /update-docs  — обновление документации
        ↓
   /close-sprint — push, обновить STATUS.md
```

Audit и e2e запускаются оба **до** fix-фазы. Потому что они ловят разные классы проблем (статические vs runtime), и нет смысла фиксить одно до того как нашёл другое.

Fix-фаза включает:
- **Quick-fixes** из audit (всё что можно поправить быстро, даже если не связано со спринтом)
- **Баги** из e2e
- **Рефакторинг** напрямую связанный с изменённым кодом (даже если большой)
- Что не влезает по связности или объёму → backlog

Fix-фаза может расти непредсказуемо — это нормально, дедлайнов по времени нет.

### Техспринты

Каждый 5-й спринт (или раньше если backlog >30 items):
- `/new-sprint` проверяет давность последнего техспринта
- Предлагает техспринт, формирует scope из backlog
- Приоритет: security → code smells → infra → nice-to-have
- Техспринт проходит тот же цикл (фазы, гейты, audit, e2e)

### /go Decision Tree

Читает STATUS.md, first match wins:

1. **No sprint or sprint COMPLETE** → `/new-sprint`
2. **Sprint exists, current phase has no task files** → `/plan-phase` (генерирует задачи)
3. **Phase has pending tasks** → `/implement` (первая pending)
4. **Phase has in_progress tasks** → `/implement` (resume)
5. **All phase tasks done, phase not COMPLETE** → `/close-phase`
6. **All feature phases COMPLETE, audit not done** → `/audit`
7. **Audit done, e2e not done** → `/e2e-run`
8. **Audit + e2e done, fix phase not created** → create fix phase from findings
9. **Fix phase exists with pending tasks** → `/implement`
10. **Fix phase COMPLETE, docs not updated** → `/update-docs`
11. **Docs updated** → `/close-sprint`
12. **Blockers exist** → Report, wait for human

### STATUS.md формат

```markdown
## Current Sprint
- **Sprint**: 001-tech-backlog-cleanup
- **Goal**: Разгрузить backlog, обкатать sprint-based pipeline
- **Type**: tech
- **Started**: 2026-04-10
- **Current Phase**: Phase 1 — PYTHONPATH + imports

## Phase Progress
| Phase | Name | Status |
|-------|------|--------|
| 0 | Split github.py | COMPLETE |
| 1 | PYTHONPATH + imports | Current |
| 2 | Audit + E2E | Pending |
| 3 | Fix phase | Pending |

## Sprint History
| # | Goal | Type | Dates | Phases |
|---|------|------|-------|--------|
```

### Sprint.md формат

```markdown
# Sprint NNN: <Title>

> **Goal**: <one sentence>
> **Type**: feature | tech
> **Started**: <date>

## Phase 0: <Name>
- <task description → file phase0-task1-slug.md>
- <task description → file phase0-task2-slug.md>

## Phase 1: <Name>
- ...

## Decisions
- <architectural choices made during sprint>

## Deferred
- <items considered but explicitly excluded → go to backlog>
```

### Task file формат (docs/sprints/NNN-slug/tasks/phaseN-taskM-slug.md)

```markdown
# Phase N Task M: <Title>

## Description
<what needs to change and why>

## Tests First
- <unit test 1>
- <unit test 2>

## Acceptance Criteria
- [ ] <criterion>
- [ ] <criterion>

## Status: pending | in_progress | done

## Developer Notes
<filled during implementation — decisions, gotchas, what changed from plan>
```

### Скиллы: новые и модифицированные

| Скилл | Статус | Что делает |
|-------|--------|-----------|
| `/go` | **NEW** | Диспетчер: читает STATUS.md, вызывает нужный скилл |
| `/new-sprint` | **NEW** | Проверяет техспринт cadence, предлагает scope из VISION/ROADMAP/backlog |
| `/plan-phase` | **NEW** | Генерирует task files для текущей фазы (2-4 задачи) |
| `/close-phase` | **NEW** | Прогоняет + пишет integration tests, проверяет все tasks done |
| `/close-sprint` | **NEW** | Финальный гейт: push, update STATUS.md |
| `/implement` | **MODIFY** | Читает STATUS.md, работает с task files вместо backlog |
| `/audit` | **MODIFY** | Проверяет VISION.md invariants, пишет в sprint e2e/ dir |
| `/triage` | **DELETE** | Функции поглощены: quick-fix → fix phase, остальное → backlog |
| `/checkpoint` | **DELETE** | Функции поглощены: docs → `/update-docs`, audit → sprint endgame |
| `/plan` | **DELETE** | Заменён на `/plan-phase` (фаза = единица планирования, не задача) |
| `/brainstorm` | **KEEP** | Свободный формат, вне спринтового ритма. Результаты маршрутизируются вручную (новый спринт, vision change, hotfix, backlog item и т.д.) |

### VISION.md

```markdown
# Vision & Architectural Invariants

## Product Vision
Autonomous code generation pipeline. User describes project in Telegram →
gets deployed project with CI/CD in 20-30 minutes. Then iterates via dialogue.

## Invariants (audit checks these)
1. Services communicate ONLY via Redis Streams or API calls. No cross-service imports.
2. All statuses are enums in shared/contracts/. No hardcoded status strings.
3. All queue messages are Pydantic DTOs in shared/contracts/queues/. No raw dicts.
4. Fail-fast everywhere. No .get(key, default), no fallback values, no silent None handling.
5. Worker = ephemeral Docker container with CLI agent. Nothing else is a "worker".
6. Secrets never reach LLM context. Use handles, Python resolves actual values.
7. Each service owns its models. shared/ contains only contracts (DTOs, enums, queue schemas).
8. Logging via structlog only. No print(). All events structured with correlation IDs.

## Non-Goals (explicit)
- Not a general-purpose CI/CD tool
- Not a code review tool
- Not a multi-language platform (Python + future Rust only)
```

---

## Action Items

- → new task: "Create VISION.md with architectural invariants"
- → new task: "Create STATUS.md + docs/sprints/ structure + task file format"
- → new task: "Create /go skill (dispatcher reading STATUS.md)"
- → new task: "Create /new-sprint skill (tech sprint cadence check + scope from VISION/ROADMAP/backlog)"
- → new task: "Create /plan-phase skill (generate task files for current phase)"
- → new task: "Create /close-phase skill (integration tests + test maintenance + phase gate)"
- → new task: "Modify /implement to work with sprint task files instead of backlog"
- → new task: "Create /close-sprint skill (push + STATUS.md update)"
- → new task: "Modify /audit to check VISION.md invariants"
- → new task: "Migrate current 47 backlog tasks to deferred pool format"
- → new task: "Delete deprecated skills: /triage, /checkpoint, /plan"
- → idea: "/meta-go for autonomous overnight sprint execution"
- → idea: "Tech sprint cadence: every 5th sprint = backlog cleanup"
