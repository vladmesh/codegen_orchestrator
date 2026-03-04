# Backlog

> **Актуально на**: 2026-03-04

## Queue (ordered by priority, first = next)

### #23 Extract Shared Code (infra_client + constants)
- **Priority**: HIGH (quick win)
- **User Story**: —
- **Plan**: —
- **Status**: pending
- **Brief**: `infra_client.py` byte-for-byte identical in langgraph and infra-service (279 LOC each) → move to `shared/clients/`. Shared constants (`Timeouts`, `Provisioning`, `Paths.SSH_KEY`) duplicated between `langgraph/config/constants.py` and `infra-service/config/constants.py` → extract to `shared/config/constants.py`. Source: audit 2026-03-04.

### #25 Post-Deploy Smoke Tester
- **Priority**: HIGH (pre-MVP)
- **User Story**: US0 (acceptance criteria: stable E2E)
- **Plan**: —
- **Status**: pending
- **Brief**: Минимальная нода в DevOps subgraph после деплоя. HTTP smoke (httpx: `/health` + эндпоинты из спеки) для бэкендов. Telethon smoke (`/start` + команды из спеки) для ботов. Детерминированные проверки, без Claude. Pass → уведомление юзеру, fail → retry/обратно в Engineering. Telethon-сессия хранится как секрет оркестратора. Brainstorm: `docs/brainstorms/qa-node.md` (полная версия — post-MVP).

### #2 Agent Hierarchy & Incident Response
- **Priority**: HIGH (post-MVP candidate)
- **User Story**: —
- **Plan**: —
- **Status**: pending
- **Brief**: TaskAssessor, Watchdog & Recovery (DockerEventsListener, DLQ consumer), shared session memory ("предсмертная записка" агента). Brainstorm: `docs/brainstorms/agent-hierarchy.md`.

### #4 CI Pipeline Redesign
- **Priority**: HIGH
- **User Story**: —
- **Plan**: —
- **Status**: partial (PR/Publish split done)
- **Brief**: Branch Protection, параллельные интеграционные тесты (GH Actions matrix). Brainstorm: `docs/brainstorms/ci-pipeline-redesign.md`.

### #7 Security Audit: Deploy Cleanup
- **Priority**: HIGH
- **User Story**: —
- **Plan**: —
- **Status**: pending
- **Brief**: Очистка зависших контейнеров/образов после деплоев (`docker image prune`). SSH hardening уже done в ansible.

### #21 Deploy Pre-Check
- **Priority**: MEDIUM
- **User Story**: —
- **Plan**: —
- **Status**: pending
- **Brief**: Валидация сервера перед деплоем. Прокинуть `action` (create/feature/fix) в DeployMessage. SSH-проверка `/opt/services/<NAME>/`. Файлы: `shared/contracts/queues/deploy.py`, `engineering_worker.py`, `deploy_worker.py`.

### #8 Workspace Failure Counter
- **Priority**: MEDIUM
- **User Story**: —
- **Plan**: docs/plans/workspace-persistence.md (phase 6)
- **Status**: pending
- **Brief**: Счётчик падений по `project_id`. Wipe workspace после 2 попыток, отклонение после 3.

### #10 Worker Lifecycle (Pause/Unpause)
- **Priority**: MEDIUM
- **User Story**: —
- **Plan**: —
- **Status**: pending
- **Brief**: `docker pause` при бездействии. CPU/RAM лимиты на контейнеры.

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
- **Brief**: Legacy networking fallback в `manager.py:525-530` и project lookup по имени в `github_sync.py:213-226` — оба оставлены как защитный код. Audit 2026-03-04: delete `services/langgraph/src/list_repos.py` (dead debug script, 72 LOC).

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
- "Добавить батарейку" к существующему проекту (источник: US3)
- Enable Ruff S110 + BLE001 rules to catch swallowed/broad exceptions (источник: audit 2026-03-04)

## Done (last 10)

- #24 Fix Critical getenv Defaults — 2026-03-04
- #6 Fix & Consolidate Test Suites — 2026-03-04
- #22 Worker Network Isolation — 2026-03-03
- #1 Service Template Simplification — 2026-02-25
- #3+#5 Redis Streams Unification + Queue Contracts — 2026-02-17
- #9 Worker Reuse for CI Fix Loop — 2026-02-19
- #14 Contract Consistency — 2026-02-17
- #15 Resolve Enum Divergence — 2026-02-25
- #16 Consolidate ServiceModule — 2026-02-25
- Secrets Encryption (Fernet) — 2026-02-15
- Caddy Reverse Proxy — 2026-02-15
- Dev Environment DinD → Native — 2026-02-20
