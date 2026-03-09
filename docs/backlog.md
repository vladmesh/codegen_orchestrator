# Backlog

> [!WARNING]
> Этот файл автогенерируется командой `make sync`. Не редактируйте вручную — изменения будут перезаписаны.

> **Updated**: 2026-03-09

## Queue (ordered by priority, first = next)

### #1001 Pipeline failure supervisor — retry, fail-fast, admin logging
- **Priority**: LOW
- **Plan**: yes (in work item)
- **Status**: backlog
- **Brief**: Dispatcher сейчас только диспатчит и завершает story. Нужно добавить третью роль — supervisor: обнаружение и обработка stuck states.  ## Scope  ### Stuck state detection (в 30с цикле dispatcher) 1. **Story в `created` дольше N минут** — architect не отработал. Retry: republish в architect:queue (...

### #1000 Smoke tests — raise project stack via worker-manager
- **Priority**: CRITICAL
- **Plan**: —
- **Status**: backlog
- **Brief**: Give workers the ability to smoke-test by raising the project stack through worker-manager (same pattern as DB provisioning).  Current state: Workers request infra (postgres, redis) via worker:commands. Worker-manager raises containers on host in dev_proj network, flat structure, no DinD.  Goal: ...

### #7 Security Audit: Deploy Cleanup
- **Priority**: LOW
- **Plan**: —
- **Status**: backlog
- **Brief**: Очистка зависших контейнеров/образов после деплоев (`docker image prune`). SSH hardening уже done в ansible. Priority adjusted by triage (roadmap phase change).

### #10 Worker Lifecycle (Pause/Unpause)
- **Priority**: LOW
- **Plan**: —
- **Status**: backlog
- **Brief**: `docker pause` при бездействии. CPU/RAM лимиты на контейнеры.

### #62 /brainstorm resume — продолжение обсуждения существующего драфта
- **Priority**: LOW
- **Plan**: —
- **Status**: backlog
- **Brief**: /brainstorm должен уметь подхватить существующий draft из БД и продолжить дискуссию. Сценарий: /brainstorm resume → GET /api/brainstorms/?status=draft → список → выбор → дополнение content. Также: миграция 14 legacy brainstorms из docs/brainstorms/ в БД (status=draft/done/triaged по текущему стат...

### Integrate Repository into production flows (webhook, scheduler, worker)
- **Priority**: LOW
- **Plan**: —
- **Status**: backlog
- **Brief**: Подключить Repository модель в production pipeline. Сейчас webhook/scheduler/worker используют Project.repository_url и Project.github_repo_id напрямую.  1. webhooks.py: lookup через Repository.provider_repo_id вместо Project.github_repo_id 2. github_sync.py: создаёт Repository записи вместо обно...

### Fix eager import chains in scaffolded projects

- **Priority**: LOW
- **Plan**: —
- **Status**: backlog
- **Brief**: __init__.py eagerly imports app/create_app, which triggers full import chain. Any broken import crashes everything including alembic. Fix: lazy imports or direct model import in env.py.

### Auto-generate routers from domain specs

- **Priority**: LOW
- **Plan**: —
- **Status**: backlog
- **Brief**: Framework generates protocols and controller stubs but routers are manual. Router pattern is formulaic — generate stubs to reduce boilerplate and prevent spec drift.

### Add predefined module to existing project (make add-module)

- **Priority**: LOW
- **Plan**: —
- **Status**: backlog
- **Brief**: Allow adding tg_bot/notifications/frontend to a project generated without them. Currently requires re-generation.

### #2 Agent Hierarchy & Incident Response
- **Priority**: LOW
- **Plan**: —
- **Status**: backlog
- **Brief**: TaskAssessor, Watchdog & Recovery (DockerEventsListener, DLQ consumer), shared session memory ("предсмертная записка" агента). Brainstorm: `docs/brainstorms/agent-hierarchy.md`. Priority adjusted by triage (roadmap phase change). NB: Watchdog/DLQ scope уменьшится — WorkItemEvent (#55) покрывает a...

### #19 Split github.py Client (986 LOC)
- **Priority**: LOW
- **Plan**: —
- **Status**: backlog
- **Brief**: Разбить на submodules по domain: repos, actions, secrets, workflows. Фасад делегирует в sub-clients.

### #20 API Key & SSH Key Encryption
- **Priority**: LOW
- **Plan**: —
- **Status**: backlog
- **Brief**: Применить SecretsCipher (Fernet) к API key values и SSH keys. TODO-комменты в `api_keys.py:36,72` и `servers.py:66`.

### Unified handlers: error handling strategy

- **Priority**: LOW
- **Plan**: —
- **Status**: backlog
- **Brief**: Define error handling for event handlers: DLQ, error events, or retries with exponential backoff.

### Auto-update __init__.py re-exports after generation

- **Priority**: LOW
- **Plan**: —
- **Status**: backlog
- **Brief**: After adding new models, schemas/__init__.py etc must be manually updated. Generate these files or remove re-export pattern.

### Context packer for agents (make context service=backend)

- **Priority**: LOW
- **Plan**: —
- **Status**: backlog
- **Brief**: Aggregate relevant spec, AGENTS.md, signatures, linter errors into single token-optimized file for agent context.

### #11 E2E Tests Completion
- **Priority**: LOW
- **Plan**: —
- **Status**: backlog
- **Brief**: Завершить покрытие E2E (Level 5-7). Добавить E2E mock-тесты (Level A+B) в CI.

### #26 Notifications via Redis Stream (убрать прямую зависимость от Telegram API)
- **Priority**: LOW
- **Plan**: —
- **Status**: backlog
- **Brief**: Сейчас `shared/notifications.py` шлёт в Telegram API напрямую — scheduler, infra-service держат `TELEGRAM_BOT_TOKEN`. Нужно: сервисы публикуют в Redis stream `notifications:queue`, telegram_bot потребляет и отправляет. Убирает `TELEGRAM_BOT_TOKEN` из всех сервисов кроме telegram_bot, упрощает тес...

### #41 Parallel Server Provisioning
- **Priority**: LOW
- **Plan**: —
- **Status**: backlog
- **Brief**: infra-service обрабатывает `provisioner:queue` последовательно — один consumer loop с `await` на каждый job (`services/infra-service/src/main.py:127-148`). При 3+ серваках в `PENDING_SETUP` каждый Ansible прогон (~15 мин) блокирует очередь. LangGraph-сторона уже параллельна (`asyncio.create_task`...

### Auto-fuzzing and contract testing (schemathesis)

- **Priority**: LOW
- **Plan**: —
- **Status**: backlog
- **Brief**: Integrate schemathesis into CI. Reads openapi.json, fuzzes running service with valid/invalid inputs. Auto-detect 500 errors without manual tests.

### Extract type mappings into language-agnostic config

- **Priority**: LOW
- **Plan**: —
- **Status**: backlog
- **Brief**: Partially done: type_spec_to_python() centralized in spec/types.py. Remaining: unify all mappings (Python, TypeScript, OpenAPI) via single table/config. Extract to YAML/TOML for adding new languages without code.

### Enum types in model field definitions

- **Priority**: LOW
- **Plan**: —
- **Status**: backlog
- **Brief**: Support enum types in YAML specs. Generated Pydantic models would use Literal or Enum instead of plain strings.

### CLI wrappers (my-framework init/sync/update)

- **Priority**: LOW
- **Plan**: —
- **Status**: backlog
- **Brief**: Wrap make commands into standalone CLI tool. Simplify usage for humans and agents.

### Celery worker support

- **Priority**: LOW
- **Plan**: —
- **Status**: backlog
- **Brief**: Add celery-worker service type. Pre-configured Redis/RabbitMQ in docker-compose, auto-generated celery_app and task decorators.

### #46 Rename duckduckgo_search → ddgs
- **Priority**: LOW
- **Plan**: —
- **Status**: backlog
- **Brief**: Пакет `duckduckgo_search` переименован в `ddgs`. Runtime warning в логах: `This package has been renamed to ddgs! Use pip install ddgs instead.` Заменить зависимость в `services/langgraph/pyproject.toml`, обновить импорт в `services/langgraph/src/po/tools.py`, перегенерировать lock-файл (`make lo...

### Audit scaffold templates for best practices

- **Priority**: LOW
- **Plan**: —
- **Status**: backlog
- **Brief**: Review templates in .framework/framework/templates/scaffold/services/ to ensure they use latest patterns adopted by main services.

### Unified handlers: transactional outbox pattern

- **Priority**: LOW
- **Plan**: —
- **Status**: backlog
- **Brief**: Events published directly after DB writes. Consider transactional outbox to avoid dual write problem.

### High-level architecture spec (connectivity graph)

- **Priority**: LOW
- **Plan**: —
- **Status**: backlog
- **Brief**: Define service relationships in services.yml: access, exposes, consumes. Generate typed clients and network policies.

### Spec-first observability (auto OpenTelemetry)

- **Priority**: LOW
- **Plan**: —
- **Status**: backlog
- **Brief**: Auto-embed traces and metrics into generated endpoints. Zero-config observability from spec definitions.

### Make YAML specs fully language-agnostic

- **Priority**: LOW
- **Plan**: —
- **Status**: backlog
- **Brief**: Partially done: abstract types used. Remaining: replace list[string] shorthand with JSON Schema array+items for full language-agnosticity.

### Spec-only module storage (long-term)

- **Priority**: LOW
- **Plan**: —
- **Status**: backlog
- **Brief**: Store only specs and minimal scaffolds, generate all business logic on project creation. Zero distinction between built-in and custom services.

### Rust PoC: backend service on Axum

- **Priority**: LOW
- **Plan**: —
- **Status**: backlog
- **Brief**: Proof of concept — Axum + SeaORM 2.0 + utoipa. Same API, same Docker, same compose as Python backend. Test how well AI agent handles Axum code generation.

### Rust PoC: Telegram bot on teloxide

- **Priority**: LOW
- **Plan**: —
- **Status**: backlog
- **Brief**: PoC Telegram bot on teloxide as alternative to python-telegram-bot. Compare developer and agent experience.

### Research Tera as Jinja2 replacement for codegen

- **Priority**: LOW
- **Plan**: —
- **Status**: backlog
- **Brief**: Tera is Rust Jinja2 analog with near-identical syntax. Evaluate how many current templates can be reused. If 90%+ compatible, migration cost is low.

### Add Rust service type to services.yml

- **Priority**: LOW
- **Plan**: —
- **Status**: backlog
- **Brief**: New rust-axum service type. Scaffold template with Cargo.toml, multi-stage Dockerfile (cargo-chef), main.rs. Enables mixing Python and Rust services.


## Done (last 10)

- #36 Architect: migrate from scheduler function to LangGraph ReAct agent — 2026-03-09
- #35 LangGraph service directory refactoring (workers→consumers) — 2026-03-08
- Architect node — story decomposition into tasks + task dispatcher — 2026-03-08
- PO tools contract tests — validate payloads against API schemas — 2026-03-09
- Decouple shared/ from Docker builds — reduce rebuild blast radius — 2026-03-08
- Fix compose.dev.yml ports conflict with orchestrator worker containers
 — 2026-03-08
- Add E2E CI job for unified handlers (dual-transport pipeline)
 — 2026-03-08
- Fix Jinja whitespace in doc templates + add cache mounts to Dockerfiles
 — 2026-03-08
- Fix codegen quality (cosmetic bugs + param types + optional schemas)
 — 2026-03-08
- Rewrite copier tests
 — 2026-03-08

## Ideas

> [!WARNING]
> **DEPRECATED**: Этот файл более не является источником правды. Идеи мигрировали в базу данных (превращаются в `brainstorms` или `work_items`). Файл временно оставлен для генерации старого backlog.md.

Manually maintained list of ideas and future improvements.
Read by `make backlog` to include in generated backlog.md.

- Project Name Collision: repo_name и deploy path строятся из `project.name`, а не `project.id` — два юзера с одинаковым именем получают один GitHub-репо, один deploy path `/opt/services/{name}/`, один Docker-образ. Фикс: включить `project_id` в repo name (`my-bot-a1b2c3d4`). Затронуты: `engineering_worker.py:517-519` (repo name gen), `github.py:940` (create_repo), `devops/nodes.py:443,348` (PROJECT_NAME secret). Post-MVP. (источник: анализ #30 multi-user isolation)

- Self-hosted GitLab или GH runner на VPS (источник: E2E failure rate 50%, 2026-03-02)
- Admin UI: projects, workers, logs (источник: MVP Phase 4)
- Tester node (полный): QA-агент с Claude + Playwright после деплоя (источник: brainstorm qa-node.md, post-MVP)
- CI Monitor Node: вынести `_wait_for_ci_and_fix` в LangGraph-ноду (источник: audit)
- API Authentication: заменить `x-telegram-id` на JWT (источник: audit)
- Telegram Bot Pool: пре-зарегистрированные боты (источник: US2)
- Cost Tracking: LLM токены per user/project (источник: roadmap Phase 6)
- Deploy Rollback: откат при failed health checks (источник: audit)
- Docker Python SDK для worker-manager (источник: audit-v2)
- Fix `sys.path` hack в telegram_bot (источник: audit)
- Split Tier 2 large files: devops/nodes.py, telegram_bot/main.py, env_analyzer.py (источник: audit-v2)
- Worker port isolation: убрать `ports:` из compose.base.yml при параллелизации (источник: audit)
- Enable Ruff S110 + BLE001 rules to catch swallowed/broad exceptions (источник: audit 2026-03-04)
- pytest-xdist для backend integration tests — исследовать после параллелизации стеков (источник: brainstorm ci-integration-test-speed)
- Split worker-manager/src/manager.py (828 LOC, 6 functions >50 LOC) (источник: audit 2026-03-05)
- infra-service unit test coverage: 9 source files, 0 tests (источник: audit 2026-03-05)
- ~~Task Store в БД~~ — поглощено #55 (WorkItem Model + API)
- ~~Миграция скиллов на API-first~~ — станет #56-58 (Steps 1-3 из brainstorm orchestrator-v2-task-management)
- Assessor node — фильтр сложности запросов, теперь на базе WorkItem (Phase 4) (источник: brainstorm epic-decomposition)
- Architect node — декомпозиция сложных задач на WorkItems (Phase 5) (источник: brainstorm epic-decomposition)
- SOPS для .env на проде (Phase 2B) (источник: brainstorm epic-decomposition)
- Zero-downtime deploy — rolling restart (Phase 2B) (источник: brainstorm epic-decomposition)
- RLS policies на PostgreSQL для multi-tenant (подготовка, не блокер для MVP) (источник: brainstorm multi-tenant-isolation)
- Redis key prefix isolation (tenant:{id}:*) — подготовка к multi-tenant (источник: brainstorm multi-tenant-isolation)
- Отдельная database для системных данных оркестратора (orchestrator_system) — Phase 3 (источник: brainstorm multi-tenant-isolation)
- Унифицировать Time4VPS credentials: infra-service читает из env vars, scheduler — из api_keys таблицы через API. Один источник правды. (источник: seed/nuke audit 2026-03-05)
- Shared Docker image layer для интеграционных тестов — собрать api/db/redis один раз, шарить между стеками через GHA artifacts (источник: brainstorm ci-integration-test-speed, Option B)
- Объединить мелкие compose-стеки (frontend 1 тест + infra 2 теста) для экономии одного up/down цикла (источник: brainstorm ci-integration-test-speed, Option D)
- CI: cache copier template clone для template integration tests — marginal gain ~10-15с, сложный cache invalidation (источник: brainstorm ci-integration-test-speed)
- Отдельный UI/UX для подтверждения собранного ТЗ пользователем перед инженерным этапом (источник: brainstorm po-smart-node)
- Functional health check: текущий healthcheck проверяет только что процесс жив (HTTP 200 на /health). Не ловит ситуации когда таблицы не созданы, миграции не прошли, seed-данные отсутствуют — бэкенд "healthy", но 500 на каждый бизнес-запрос. Можно добавить в service-template readiness probe с `SELECT 1` или проверкой ключевых таблиц (источник: fortune-telling-bot — backend healthy, но relation "fortunes" does not exist)
