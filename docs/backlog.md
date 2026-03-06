# Backlog

> **Актуально на**: 2026-03-06

## Queue (ordered by priority, first = next)

### #33 Secrets Hygiene
- **Priority**: HIGH
- **User Story**: —
- **Plan**: —
- **Status**: pending
- **Brief**: Убрать `secrets/github_app.pem` из git + .gitignore (deploy.yml уже пишет из секрета). Dedicated SSH key вместо host `~/.ssh` — генерировать `ORCHESTRATOR_SSH_PRIVATE_KEY`, хранить как GitHub Secret, монтировать `/opt/secrets/orchestrator_ssh_key`. Источник: brainstorm `docs/brainstorms/epic-decomposition.md`.

### #34 US3: Add Feature to Existing Project
- **Priority**: HIGH
- **User Story**: —
- **Plan**: —
- **Status**: pending
- **Brief**: Core product flow — "допили мне бота". 4 части: (1) PO tool: select existing project (`list_projects(user_id=X)` + выбор), (2) Engineering worker: feature flow (git pull → branch → code → CI, без scaffold), (3) Deploy: redeploy existing (тот же flow без allocation), (4) E2E test: feature-add scenario. Кандидат на первый "эпик". Источник: brainstorm `docs/brainstorms/epic-decomposition.md`.

### #39 Enforce Project-User Binding (owner_id NOT NULL)
- **Priority**: MEDIUM
- **User Story**: —
- **Plan**: —
- **Status**: pending
- **Brief**: Проект строго привязан к юзеру. `owner_id` становится NOT NULL. github_sync перестаёт создавать проекты в БД — вместо этого шлёт warning админам через `notify_admins` о найденных ничейных репо. Админы разбираются вручную.
- **Findings**:
  - **Модель**: `shared/models/project.py:33` — `owner_id: Mapped[int | None]`, nullable FK
  - **DTO**: `shared/contracts/dto/project.py:69,85` — `owner_id: int | None = None`
  - **API create**: `services/api/src/routers/projects.py:84-93` — owner_id опционален, без `X-Telegram-ID` = None
  - **github_sync**: `services/scheduler/src/tasks/github_sync.py:213-227` — `_sync_single_repo` создаёт `ProjectCreate` без owner → проект без владельца в БД
  - **Webhook**: `services/api/src/routers/webhooks.py:113` — `if project.owner_id` guard перед нотификацией
  - **Тесты**: `test_create_project.py:55` — тест "без X-Telegram-ID → owner_id=None" нужно инвертировать (должен быть 4xx)
- **Scope**:
  1. Миграция: `owner_id` NOT NULL (backfill existing orphans или удалить)
  2. DTO: `owner_id: int` (required) в `ProjectCreate`; убрать None из `ProjectUpdate.owner_id`
  3. API: `POST /api/projects/` — 400 если нет `X-Telegram-ID`
  4. github_sync `_sync_single_repo`: если проект не найден в БД — `notify_admins` warning вместо `api_client.create_project`; doc sync для существующих проектов остаётся
  5. Webhook: убрать `if project.owner_id` guard (всегда есть)
  6. Тесты: обновить unit/service тесты

### #8 Workspace Failure Counter
- **Priority**: MEDIUM
- **User Story**: —
- **Plan**: docs/plans/workspace-persistence.md (phase 6)
- **Status**: pending
- **Brief**: Счётчик падений по `project_id`. Wipe workspace после 2 попыток, отклонение после 3.

### #21 Deploy Pre-Check
- **Priority**: MEDIUM
- **User Story**: —
- **Plan**: —
- **Status**: pending
- **Brief**: Валидация сервера перед деплоем. Прокинуть `action` (create/feature/fix) в DeployMessage. SSH-проверка `/opt/services/<NAME>/`. Файлы: `shared/contracts/queues/deploy.py`, `engineering_worker.py`, `deploy_worker.py`.

### #7 Security Audit: Deploy Cleanup
- **Priority**: MEDIUM
- **User Story**: —
- **Plan**: —
- **Status**: pending
- **Brief**: Очистка зависших контейнеров/образов после деплоев (`docker image prune`). SSH hardening уже done в ansible. Priority adjusted by triage (roadmap phase change).

### #10 Worker Lifecycle (Pause/Unpause)
- **Priority**: MEDIUM
- **User Story**: —
- **Plan**: —
- **Status**: pending
- **Brief**: `docker pause` при бездействии. CPU/RAM лимиты на контейнеры.

### #2 Agent Hierarchy & Incident Response
- **Priority**: MEDIUM
- **User Story**: —
- **Plan**: —
- **Status**: pending
- **Brief**: TaskAssessor, Watchdog & Recovery (DockerEventsListener, DLQ consumer), shared session memory ("предсмертная записка" агента). Brainstorm: `docs/brainstorms/agent-hierarchy.md`. Priority adjusted by triage (roadmap phase change).

### #18 Split engineering_worker.py (1088 LOC)
- **Priority**: MEDIUM
- **User Story**: —
- **Plan**: —
- **Status**: pending
- **Brief**: Вынести фазы (scaffold, CI fix loop, deploy trigger) в отдельные модули.

### #19 Split github.py Client (986 LOC)
- **Priority**: MEDIUM
- **User Story**: —
- **Plan**: —
- **Status**: pending
- **Brief**: Разбить на submodules по domain: repos, actions, secrets, workflows. Фасад делегирует в sub-clients.

### #20 API Key & SSH Key Encryption
- **Priority**: MEDIUM
- **User Story**: —
- **Plan**: —
- **Status**: pending
- **Brief**: Применить SecretsCipher (Fernet) к API key values и SSH keys. TODO-комменты в `api_keys.py:36,72` и `servers.py:66`.

### #11 E2E Tests Completion
- **Priority**: MEDIUM
- **User Story**: —
- **Plan**: —
- **Status**: pending
- **Brief**: Завершить покрытие E2E (Level 5-7). Добавить E2E mock-тесты (Level A+B) в CI.

### #26 Notifications via Redis Stream (убрать прямую зависимость от Telegram API)
- **Priority**: MEDIUM
- **User Story**: —
- **Plan**: —
- **Status**: pending
- **Brief**: Сейчас `shared/notifications.py` шлёт в Telegram API напрямую — scheduler, infra-service держат `TELEGRAM_BOT_TOKEN`. Нужно: сервисы публикуют в Redis stream `notifications:queue`, telegram_bot потребляет и отправляет. Убирает `TELEGRAM_BOT_TOKEN` из всех сервисов кроме telegram_bot, упрощает тесты, единая точка отправки. Источник: #24 code review.

## Ideas

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
- Task Store в БД — Epic, WorkItem, WorkItemGate с tenant_id с первого дня (dogfooding для продукта, Phase 3) (источник: brainstorms epic-decomposition, multi-tenant-isolation)
- Миграция скиллов на API-first (/next, /implement, /triage через API) (источник: brainstorm epic-decomposition)
- Assessor node — фильтр сложности запросов (Phase 4) (источник: brainstorm epic-decomposition)
- Architect node — декомпозиция сложных задач через Task Store (Phase 5) (источник: brainstorm epic-decomposition)
- SOPS для .env на проде (Phase 2B) (источник: brainstorm epic-decomposition)
- Zero-downtime deploy — rolling restart (Phase 2B) (источник: brainstorm epic-decomposition)
- RLS policies на PostgreSQL для multi-tenant (подготовка, не блокер для MVP) (источник: brainstorm multi-tenant-isolation)
- Redis key prefix isolation (tenant:{id}:*) — подготовка к multi-tenant (источник: brainstorm multi-tenant-isolation)
- Отдельная database для системных данных оркестратора (orchestrator_system) — Phase 3 (источник: brainstorm multi-tenant-isolation)
- Унифицировать Time4VPS credentials: infra-service читает из env vars, scheduler — из api_keys таблицы через API. Один источник правды. (источник: seed/nuke audit 2026-03-05)
- Shared Docker image layer для интеграционных тестов — собрать api/db/redis один раз, шарить между стеками через GHA artifacts (источник: brainstorm ci-integration-test-speed, Option B)
- Объединить мелкие compose-стеки (frontend 1 тест + infra 2 теста) для экономии одного up/down цикла (источник: brainstorm ci-integration-test-speed, Option D)
- CI: cache copier template clone для template integration tests — marginal gain ~10-15с, сложный cache invalidation (источник: brainstorm ci-integration-test-speed)

## Done (last 10)

- #32 Prod Deploy Pipeline — 2026-03-06
- #38 Fix Service Integration Tests After Multi-User Isolation — 2026-03-06
- #31 Port Allocation Locking — 2026-03-05
- #30 Multi-user Isolation Fix — 2026-03-05
- #12 Documentation Cleanup (Zavhoz + Deploy-worker) — 2026-03-05
- #4 CI Pipeline Redesign — 2026-03-05
- #17 Dead Code & Legacy Cleanup — 2026-03-05
- #37 Remove Dead LLM Agent Configs from Code — 2026-03-05
- #36 Remove CLI Agent Config Infrastructure — 2026-03-05
- #29 Fix ORCHESTRATOR_USER_ID defaults in CLI commands — 2026-03-05
- #35 [meta] E2E Skill: server IP resolution + repo slug paths — fixed 2026-03-05
