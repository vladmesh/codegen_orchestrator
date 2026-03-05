# Backlog

> **Актуально на**: 2026-03-05

## Queue (ordered by priority, first = next)

### #30 Multi-user Isolation Fix
- **Priority**: HIGH
- **User Story**: —
- **Plan**: —
- **Status**: pending
- **Brief**: API auth bypass: без `X-Telegram-ID` header возвращает ВСЕ проекты всех пользователей (projects.py, tasks.py, allocations.py). Worker ownership: engineering_worker и deploy_worker не проверяют что user_id владеет project_id. Task update bypass: невалидный telegram_id проходит проверку (silent pass при user=None). Источник: brainstorm `docs/brainstorms/epic-decomposition.md`.

### #27 PO Tools: Pass user_id to API (owner_id bug)
- **Priority**: HIGH
- **User Story**: —
- **Plan**: —
- **Status**: pending
- **Brief**: PO tools (`services/langgraph/src/po/tools.py`) не передают `X-Telegram-ID` заголовок при вызовах API. В результате `create_project` создаёт проекты с `owner_id = NULL` — нет привязки к пользователю, `list_projects` возвращает всё всем. Фикс: прокинуть `user_id` из `config["configurable"]` в httpx-клиент как `X-Telegram-ID` header (per-request или при инициализации). Источник: e2e-run PO integration analysis, 2026-03-04. Priority adjusted by triage (roadmap phase change).

### #31 Port Allocation Locking
- **Priority**: HIGH
- **User Story**: —
- **Plan**: —
- **Status**: pending
- **Brief**: Два параллельных деплоя → оба читают порты → дупликат. `tools/allocator.py:54-122` — нет атомарности. Нужно: atomic allocate-or-fail. Источник: brainstorm `docs/brainstorms/epic-decomposition.md`.

### #32 Prod Deploy Pipeline
- **Priority**: HIGH
- **User Story**: —
- **Plan**: —
- **Status**: pending
- **Brief**: Протестировать deploy.yml end-to-end на прод-сервере. Worker base images build в pipeline (не compose-сервисы, строятся отдельно). DB backup cron (pg_dump ежедневно). Environment guard на `make nuke`. Источник: brainstorm `docs/brainstorms/epic-decomposition.md`.

### #33 Secrets Hygiene
- **Priority**: HIGH
- **User Story**: —
- **Plan**: —
- **Status**: pending
- **Brief**: Убрать `secrets/github_app.pem` из git + .gitignore (deploy.yml уже пишет из секрета). Dedicated SSH key вместо host `~/.ssh` — генерировать `ORCHESTRATOR_SSH_PRIVATE_KEY`, хранить как GitHub Secret, монтировать `/opt/secrets/orchestrator_ssh_key`. Источник: brainstorm `docs/brainstorms/epic-decomposition.md`.

### #29 Fix ORCHESTRATOR_USER_ID defaults in CLI commands
- **Priority**: HIGH
- **User Story**: —
- **Plan**: —
- **Status**: pending
- **Brief**: 3 CLI command files (`engineering.py:21`, `deploy.py:21`, `respond.py:32`) default `ORCHESTRATOR_USER_ID` to `"unknown"` — breaks audit trail. Should fail fast with `RuntimeError`. Source: audit 2026-03-05.

### #34 US3: Add Feature to Existing Project
- **Priority**: HIGH
- **User Story**: —
- **Plan**: —
- **Status**: pending
- **Brief**: Core product flow — "допили мне бота". 4 части: (1) PO tool: select existing project (`list_projects(user_id=X)` + выбор), (2) Engineering worker: feature flow (git pull → branch → code → CI, без scaffold), (3) Deploy: redeploy existing (тот же flow без allocation), (4) E2E test: feature-add scenario. Кандидат на первый "эпик". Источник: brainstorm `docs/brainstorms/epic-decomposition.md`.

### #25 Post-Deploy Smoke Tester [regression]
- **Priority**: HIGH
- **User Story**: —
- **Plan**: —
- **Status**: pending
- **Brief**: smoke_result is null в deploy task todo_api-20260305 — smoke tester не вернул результат, хотя deploy успешен. Задача была завершена 2026-03-05, но E2E показал что результат не пробрасывается. Нужно проверить deploy-worker логи, убедиться что smoke tester вызывается и результат сохраняется в task result. Источник: E2E report `docs/e2e_results/todo_api-20260305.md` Problem 3.

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

### #4 CI Pipeline Redesign
- **Priority**: MEDIUM
- **User Story**: —
- **Plan**: —
- **Status**: partial (PR/Publish split done)
- **Brief**: Branch Protection, параллельные интеграционные тесты (GH Actions matrix). Split `test-integration` into 5 parallel jobs (backend, cli, template, frontend, infra) — wall-clock 10min→3-4min. Brainstorms: `docs/brainstorms/ci-pipeline-redesign.md`, `docs/brainstorms/ci-integration-test-speed.md`. Priority adjusted by triage (roadmap phase change).

### #2 Agent Hierarchy & Incident Response
- **Priority**: MEDIUM
- **User Story**: —
- **Plan**: —
- **Status**: pending
- **Brief**: TaskAssessor, Watchdog & Recovery (DockerEventsListener, DLQ consumer), shared session memory ("предсмертная записка" агента). Brainstorm: `docs/brainstorms/agent-hierarchy.md`. Priority adjusted by triage (roadmap phase change).

### #35 [meta] E2E Skill: Use Repo Slug for Server Paths
- **Priority**: MEDIUM
- **User Story**: —
- **Plan**: —
- **Status**: pending
- **Brief**: E2E skill uses `$PROJECT_NAME` (todo_api) for server paths, but deployed directory is `todo-api` (hyphenated, matches repo slug). `ssh root@$SERVER_IP "cd /opt/services/$PROJECT_NAME/infra"` fails. Fix: use `$REPO_SLUG` (hyphenated) for server directory paths. Источник: E2E report `docs/e2e_results/todo_api-20260305.md` Problem 4.

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

### #17 Dead Code & Legacy Cleanup
- **Priority**: MEDIUM
- **User Story**: —
- **Plan**: —
- **Status**: partial
- **Brief**: Legacy networking fallback в `manager.py:525-530` и project lookup по имени в `github_sync.py:213-226` — оба оставлены как защитный код. Audit 2026-03-04: delete `services/langgraph/src/list_repos.py` (dead debug script, 72 LOC). Audit 2026-03-05: move `services/langgraph/src/tests/test_architect_routing.py` to `tests/` directory.

### #12 Remove Obsolete Zavhoz
- **Priority**: MEDIUM
- **User Story**: —
- **Plan**: —
- **Status**: pending
- **Brief**: Удалить Zavhoz из документации и конфигурации, заменён на ResourceAllocatorNode.

### #13 Fix Deploy-worker Documentation
- **Priority**: MEDIUM
- **User Story**: —
- **Plan**: —
- **Status**: pending
- **Brief**: Отразить что deploy-worker и engineering-worker — процессы LangGraph, не суб-сервисы.

### #28 CI: Cache copier template for integration tests
- **Priority**: MEDIUM
- **User Story**: —
- **Plan**: —
- **Status**: pending
- **Brief**: Template integration tests clone from GitHub on every run. Pass local path instead of GitHub URL to avoid network dependency and save ~10-15s per test. Source: brainstorm `docs/brainstorms/ci-integration-test-speed.md`.

### #26 Notifications via Redis Stream (убрать прямую зависимость от Telegram API)
- **Priority**: MEDIUM
- **User Story**: —
- **Plan**: —
- **Status**: pending
- **Brief**: Сейчас `shared/notifications.py` шлёт в Telegram API напрямую — scheduler, infra-service держат `TELEGRAM_BOT_TOKEN`. Нужно: сервисы публикуют в Redis stream `notifications:queue`, telegram_bot потребляет и отправляет. Убирает `TELEGRAM_BOT_TOKEN` из всех сервисов кроме telegram_bot, упрощает тесты, единая точка отправки. Источник: #24 code review.

## Ideas

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
- Task Store в БД — Epic, WorkItem, WorkItemGate (dogfooding для продукта, Phase 3) (источник: brainstorm epic-decomposition)
- Миграция скиллов на API-first (/next, /implement, /triage через API) (источник: brainstorm epic-decomposition)
- Assessor node — фильтр сложности запросов (Phase 4) (источник: brainstorm epic-decomposition)
- Architect node — декомпозиция сложных задач через Task Store (Phase 5) (источник: brainstorm epic-decomposition)
- SOPS для .env на проде (Phase 2B) (источник: brainstorm epic-decomposition)
- Zero-downtime deploy — rolling restart (Phase 2B) (источник: brainstorm epic-decomposition)

## Done (last 10)

- #23 Extract Shared Code (infra_client + constants) — 2026-03-05
- #24 Fix Critical getenv Defaults — 2026-03-04
- #6 Fix & Consolidate Test Suites — 2026-03-04
- #22 Worker Network Isolation — 2026-03-03
- #1 Service Template Simplification — 2026-02-25
- #3+#5 Redis Streams Unification + Queue Contracts — 2026-02-17
- #9 Worker Reuse for CI Fix Loop — 2026-02-19
- #14 Contract Consistency — 2026-02-17
- #15 Resolve Enum Divergence — 2026-02-25
- #16 Consolidate ServiceModule — 2026-02-25
