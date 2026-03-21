---
id: bs-5a5de928
status: triaged
title: "Dev Pipeline 1B — MVP Phases"
created_at: 2026-03-07T12:37:04.493490Z
---

# Брейншторм: Очистка документации, Roadmap и Процесс разработки

> **Дата**: 2026-03-03
> **Контекст**: Сессия по ревизии всей документации проекта, определению MVP-скоупа и формализации процесса агентной разработки.
> **Status**: triaged

---

## 1. Обзор текущей документации

Документация разбита на блоки:
1. **Core (Root)**: `README.md`, `ARCHITECTURE.md`, `AGENTS.md`, `CLAUDE.md`
2. **References (`docs/`)**: `CONTRACTS.md`, `ERROR_HANDLING.md`, `GLOSSARY.md`, `LOGGING.md`, `NODES.md`, `SECRETS.md`, `TESTING.md`, `USER_STORIES.md`
3. **Planning & Status**: `STATUS.md`, `backlog.md`
4. **History & Ideas**: `docs/brainstorms/`, `docs/plans/`, `docs/investigations/`, `docs/playbooks/`

**Проблема:** Папки *brainstorms*, *plans* и *investigations* содержат десятки старых файлов, которые уже выполнены или утратили актуальность. Это усложняет навигацию и загрязняет контекст агентов (лишние токены).

---

## 2. План очистки документации

### 2.1. `README.md`
- Удалить упоминание репозитория `prod_infra` (заменён на ansible-роли и Deploy Worker).
- Убрать/поправить упоминания старых сервисов (`preparer` → `Scaffolder`).

### 2.2. `docs/backlog.md` и `docs/STATUS.md`
- Вычистить давно выполненные задачи (Done), чтобы не тратить токены (например: #1, #3, #5, #9, #14, #15, #16).
- Интегрировать суть оставшихся актуальных планов и брейнштормов напрямую в `docs/backlog.md` как пункты-идеи или в `docs/ROADMAP.md`.

### 2.3. Удаление устаревших файлов

**`docs/plans/` — удалить выполненные:**
- `po-react-agent.md`, `redis-streams-unification.md`, `service-template-simplification.md`, `deploy-architecture.md`, `worker-reuse-ci-fix.md`
- Оставшиеся актуальные черновики (`worker-network-isolation.md`, `workspace-persistence.md`) → консолидировать в бэклог, затем тоже удалить.

**`docs/brainstorms/` — удалить выполненные/отменённые:**
- `service-template-and-dev-environment.md`, `ci-pipeline-redesign.md`, `deploy-architecture.md`, `integration-test-speedup.md`
- Суть из актуальных (`agent-hierarchy.md`, `worker-db-isolation.md`, `worker-workspace-persistence.md`) → перенести в бэклог.

**`docs/investigations/` — удалить все 11 файлов.** Это логи починки багов (`e2e-iter10-deploy-bugs.md`, `ghcr-403-app-token.md` и т.д.), полезные только в момент расследования.

---

## 3. Roadmap и MVP

### Phase 1: Foundation & Observability (Pre-MVP)
Прежде чем пускать реальных пользователей — стабильность и видимость.

- **Fix Current Bugs (Изоляция)**: Разделение сетей воркеров (`codegen_worker`) от БД оркестратора (текущая задача #22).
- **Admin UI & Observability** *(дискуссионно → Must Have)*: Без логгирования и админки отладка параллельных пользователей будет адом. Нужен минимальный Admin UI (воркеры, статусы, логи) + трассировки LangGraph.
- **Parallel Execution Debugging**: Убедиться, что одновременные генерации не ломают друг друга (нет state leaks, нормальная маршрутизация).

### Phase 2: Quality & Boundaries (MVP Validation)
Чтобы пользователи не получали "грязь" и не ломали агентов.

- **Tester Node (базовая версия)**: Агент-тестировщик, валидирующий код перед деплоем.
- **Scope Control: Assessor Node** *(вместо Архитектора на старте)*: «Оценивальщик» смотрит промпт и реджектит слишком сложные запросы. Проще полноценного Архитектора.

---
🔥 **MVP CUTOFF POINT** 🔥
*Здесь пускаем первых закрытых бета-юзеров: тг-боты генерируются, параллельность не ломается, всё видно в логах, слишком сложные запросы отклоняются.*

---

### Phase 3: Capabilities Expansion (Post-MVP)
- **Frontend Battery** (`service_template`): Добавление фронтенда (React/Vue/HTML). Оркестратор генерирует не только ботов, но и сайты (→ US6).
- **Architect Node**: Декомпозиция сложных задач (которые ранее реджектил Assessor) на подзадачи.

### Phase 4: Scale & Advanced (Post-MVP)
- **Worker Node Swarm**: Docker-воркеры на отдельных VPS (10-20+ параллельных сборок).
- **Pro Nodes**: Analytics & Brainstorm ноды для предварительного анализа и генерации архитектуры.
- **Production Optimizations**: CDN, кэширование, auto security audit.

### Дискуссионные вопросы
1. **Админка**: Must Have для MVP или можно обойтись? *Консенсус: Must Have — без неё траблшутинг невозможен.*
2. **Нода архитектора vs Assessor**: *Консенсус: MVP = Assessor (простой фильтр), архитектор — Phase 3.*
3. **Frontend battery**: *Консенсус: Post-MVP Phase 1, но в роадмап.*

---

## 4. Процесс агентной разработки

### 4.1. Единый источник правды (State)
Агент и человек полагаются на одни и те же артефакты:
- **`STATUS.md`**: Что делаем *прямо сейчас* (Current Focus).
- **`docs/ROADMAP.md`**: Стратегический план (Фазы, MVP).
- **`docs/backlog.md`**: Бэклог задач (High/Medium/Low, Ideas).

### 4.2. Как задачи попадают в работу
1. **Идея / Баг** → заносится в `docs/backlog.md` (человеком или агентом после e2e).
2. **Сессия планирования** → человек просит агента пересмотреть бэклог, объединить дубликаты, приоритизировать, выбрать 1-2 задачи для `STATUS.md`.
3. **Брейншторм** → если задача сложная, агент пишет `docs/plans/<feature>.md`, согласуется с человеком.

### 4.3. Инструкции для агентов
- **`AGENTS.md`**: Общие технические правила (TDD, env vars, LangGraph ноды).
- **`.claude/skills/`**: Автоматизированные workflow-инструкции:
  - `e2e-check`, `e2e-run`, `e2e-cleanup` — уже есть.
  - `process-planning` — обновление бэклога и переход к следующей задаче *(планируется)*.
  - `process-audit` — аудит архитектуры, поиск dead code *(планируется)*.

### 4.4. Development Flow (цикл работы над задачей)

**Этап 1: Planning**
- Агент читает `STATUS.md`, берёт "Current Focus".
- Читает контракты (`docs/CONTRACTS.md`) и релевантный код.
- Если масштабно — пишет `docs/plans/<feature>.md`.

**Этап 2: Execution (TDD)**
- Red → Green → Refactor (строго по `AGENTS.md`).
- Изменения контрактов (API/Queues) требуют согласования с человеком.

**Этап 3: Verification (E2E)**
- Если затронуто ядро — `.claude/skills/e2e-run` (Level A/B/C).

**Этап 4: Reports & Error Routing**
- **Успех**: Пометить задачу в `STATUS.md`, убрать из `backlog.md`.
- **Баг**: Классифицировать (orchestrator/template), написать отчёт в `docs/investigations/`, предложить план починки.

### 4.5. Мета-цель
Этот процесс (Planning → TDD → E2E → Report) мы сначала откатываем на себе при разработке самого оркестратора. Потом лучшие практики инкапсулируем в LangGraph-ноды (PO → Architect → TaskAssessor → Engineering → QA), чтобы оркестратор строил пользовательские проекты по тем же рельсам.