# Backlog

> [!WARNING]
> Этот файл автогенерируется командой `make sync`. Не редактируйте вручную — изменения будут перезаписаны.

> **Updated**: 2026-03-19

## Queue (ordered by priority, first = next)

### Fix noqa suppressions that mask real complexity
- **Priority**: LOW
- **Plan**: —
- **Status**: backlog
- **Brief**: Audit found noqa comments that should be fixed instead of suppressed: PLR0913 in engineering.py:682 (too many args — extract params dataclass), PLR0911 in devops/nodes.py:144 (too many returns — extract lookup table), PLR2004 in debug.py:65 (use named constant), S110 in debug.py:71 (bare except —...

### #1005 Standardize PYTHONPATH and import patterns across service-template services
- **Priority**: LOW
- **Plan**: —
- **Status**: backlog
- **Brief**: Currently tg_bot uses PYTHONPATH=/app:/app/services/tg_bot/src (allowing relative imports) while backend and notifications_worker use PYTHONPATH=/app (requiring fully qualified imports like services.backend.src.module). This inconsistency causes coding agents to guess wrong import patterns, leadi...

### debug test
- **Priority**: CRITICAL
- **Plan**: —
- **Status**: backlog

### Add TTL/cleanup for stale Redis queue messages
- **Priority**: CRITICAL
- **Plan**: —
- **Status**: backlog
- **Brief**: Queue messages from failed/completed stories accumulate in architect:queue (and potentially other queues) with no expiry or cleanup mechanism. During the 2026-03-13 escort, 75 stale messages were found blocking a real story for hours.  Required: 1. Add periodic cleanup in scheduler: scan queue me...

### API authorization: scope worker access, protect destructive endpoints
- **Priority**: CRITICAL
- **Plan**: —
- **Status**: backlog
- **Brief**: The API is almost entirely open — no auth on tasks, stories, projects endpoints. Servers/allocations have optional admin check that skips if no header sent. Currently safe only because API listens on localhost and Caddy only proxies /webhooks/* and /v2/*. But inside the Docker network any contain...

### Refactor shared: eliminate orchestrator code from worker containers
- **Priority**: CRITICAL
- **Plan**: —
- **Status**: backlog
- **Brief**: ## Problem  Worker containers copy the entire orchestrator shared/ package into /app/shared. This conflicts with user projects that also have a shared/ directory (different package, same name). Workers hit ModuleNotFoundError or import the wrong module.  ## Current state  - worker-wrapper needs: ...

### #1017 Container drift detection via cadvisor (orphans/ghosts in health_checker)
- **Priority**: LOW
- **Plan**: —
- **Status**: backlog
- **Brief**: In health_checker: compare cadvisor container list with applications from API. Orphan (on server, not in DB) → warning in admin UI on server card. Ghost (RUNNING in DB, no container) → update status to DOWN + warning in admin UI. Source: brainstorm bs-69482380, Phase 3.

### #1018 Daily SSH job: filesystem drift check + docker prune
- **Priority**: LOW
- **Plan**: —
- **Status**: backlog
- **Brief**: Single SSH connection per server, once daily. Check /opt/projects/ vs API — orphan dirs → warning in structlog (NOT admin UI). Docker system prune -af --filter until=72h + docker volume prune -f. Source: brainstorm bs-69482380, Phase 3.

### #7 Security Audit: Deploy Cleanup
- **Priority**: LOW
- **Plan**: yes (in work item)
- **Status**: backlog
- **Brief**: Очистка зависших контейнеров/образов после деплоев (`docker image prune`). SSH hardening уже done в ansible. Priority adjusted by triage (roadmap phase change).

### #10 Worker Lifecycle (Pause/Unpause)
- **Priority**: LOW
- **Plan**: —
- **Status**: backlog
- **Brief**: `docker pause` при бездействии. CPU/RAM лимиты на контейнеры.

### Regression E2E: acceptance criteria on Repository + QA report in admin UI
- **Priority**: LOW
- **Plan**: yes (in work item)
- **Status**: backlog
- **Brief**: ## Problem  Сейчас кнопка "Run E2E" в админке на странице Application — чёрная дыра:  1. **QA не знает что тестировать.** При standalone-запуске (из админки) `story_id` пустой → QA-промпт не получает бизнес-требований. Claude Code проверяет только "живо ли приложение" (health 200, контейнеры heal...

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

### #1003 Integration test: scheduler-langgraph story worker lifecycle
- **Priority**: LOW
- **Plan**: —
- **Status**: backlog
- **Brief**: Create integration test compose (scheduler + langgraph + Redis) that verifies the cross-service story worker flow: dispatcher sends story_id in EngineeringMessage -> consumer reads it, spawns worker, stores in registry -> dispatcher cleanup on story complete removes worker.

### Allocate ports only for modules that need host exposure
- **Priority**: LOW
- **Plan**: —
- **Status**: backlog
- **Brief**: Currently ensure_project_allocations allocates a port for every module in the list (e.g. backend + tg_bot). But tg_bot does not listen on a host port — it connects outbound to Telegram API. Allocating a port for it wastes the resource and clutters the admin UI.  Fix: modules should declare whethe...


## Done (last 10)

- #1030 Decouple QA consumer from story lifecycle — 2026-03-19
- #1026 Admin UI: action buttons on entity pages — 2026-03-19
- #1025 Admin UI: Settings page (config + prompt editor) — 2026-03-19
- #1024 Thin API endpoints for admin actions (7 endpoints) — 2026-03-19
- #1023 Queue contracts: Optional story_id + action field in DeployMessage/QAMessage — 2026-03-19
- #1020 SystemConfig: model + API + ConfigStore + switch services to DB configs — 2026-03-19
- Unify worker result API — single /result endpoint, stdout capture, auto-resume — 2026-03-19
- Refactor engineering_status to StrEnum — 2026-03-19
- Restore Makefile overrides in worker-wrapper (make migrate broken) — 2026-03-19
- QA consumer: resolve by application_id, replace dicts with DTOs — 2026-03-19
