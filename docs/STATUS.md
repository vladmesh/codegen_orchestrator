# Project Status

> **Current Focus**: Native Dev Environment + Workspace Persistence
> **Plans**: [dev-env-architecture.md](./plans/dev-env-architecture.md), [workspace-persistence.md](./plans/workspace-persistence.md)

## Previous: PO ReactAgent Migration — Done

Plan: [po-react-agent.md](./plans/po-react-agent.md) — fully implemented and merged.

## Previous: Deploy Architecture — Done

Plan: [deploy-architecture.md](./plans/deploy-architecture.md) — 9 iterations completed and merged.

## Current: Native Dev Environment + Workspace Persistence

Замена Docker-in-Docker на Flat Dev Environment (bind-mounted workspaces, dual-network, compose proxy). Workspace persistence по `project_id` для resume между попытками.

### Dev Environment ([dev-env-architecture.md](./plans/dev-env-architecture.md))

- [x] Phase 1: Workspace bind-mount + dual-network
- [x] Phase 2: Compose proxy + validation hardening
- [x] Phase 3: Remove DinD capability, native tooling in worker-base
- [x] Phase 4: Orphaned resource GC

### Workspace Persistence ([workspace-persistence.md](./plans/workspace-persistence.md))

- [x] Phase 1: project_id passthrough (contract → DeveloperNode → worker-manager)
- [x] Phase 2: Workspace by project_id + git token refresh
- [x] Phase 3: PROGRESS.md instructions + resume detection
- [x] Phase 4: Workspace GC by age (24h)
- [x] Phase 5: Project mutex (one worker per project)
- [ ] Phase 6: Failure counter + retry limit (backlog)

## Previous: Deploy Architecture

E2E тест выявил что Ansible-деплой не работает. Переходим на GitHub Actions deploy + Fernet-шифрование секретов + env resolver pipeline.

9 итераций: Fernet crypto → Env groups → DeployerNode via GH Actions → Cleanup infra-service → Feature deploy flow → E2E → Self-hosted registry + Caddy → Cascade failure fixes → Registry secrets + PO hallucination fixes.

### Status

- [x] Iteration 1: Fernet encryption для секретов
- [x] Iteration 2: Env Resolver с группами
- [x] Iteration 3: DeployerNode → GitHub Actions
- [x] Iteration 4: Очистка infra-service от deploy-кода
- [x] Iteration 5: Feature deploy via GitHub webhook
- [x] Iteration 6: E2E → выявлены проблемы GHCR → iterations 7-9
- [x] Iteration 7: Self-hosted Docker Registry + Caddy TLS
- [x] Iteration 8: Cascade failure fixes (commit_sha, CI gate, sandbox)
- [x] Iteration 9: Registry secrets, CI poll race, PO hallucination

#### Iteration 1 — Done (2026-02-16)

Реализовано:
- `shared/crypto.py` — `SecretsCipher` (Fernet), `encrypt_dict`/`decrypt_dict`
- Graceful degradation: plaintext значения проходят через decrypt без ошибок (warning в лог)
- Encrypt-on-write миграция: при первой записи все старые plaintext мигрируют в encrypted
- Интеграция в 3 точках: `SecretResolverNode`, `set_project_secret` (PO tool), `set_secret_async` (CLI)
- `SECRETS_ENCRYPTION_KEY` проброшен в docker-compose (4 сервиса) + worker containers
- 10 unit-тестов для crypto, 2 для SecretResolver encryption, 1 для PO tools encryption
- E2E проверен на живом стеке: все секреты `reverse-bot` мигрировали в `gAAAAA...`

#### Iteration 2 — Done (2026-02-16)

Реализовано:
- `env_groups.py` — `EnvGroup` ABC, `PostgresGroup` (DATABASE_URL, ASYNC_DATABASE_URL, POSTGRES_PASSWORD/USER/DB), `RedisGroup` (REDIS_URL)
- `resolve_with_groups()` — диспетчер: группы резолвят атомарно → remaining идёт в fallback
- `SecretResolverNode.run()` — двухфазная резолюция: cached → groups (когерентные пароли) → fallback
- `_generate_infra_secret()` упрощён: убраны ветки DATABASE_URL, POSTGRES_*, REDIS_URL (теперь в группах)
- 9 unit-тестов для env_groups, 4 интеграционных для SecretResolver+groups
- E2E на живом стеке: пароли когерентны, кеш работает, mixed scenario корректен
- Пункты 2.5–2.6 (парсинг комментариев, compose-контекст для LLM) отложены

#### Iteration 3 — Done (2026-02-16)

Реализовано:
- `GitHubAppClient.trigger_workflow_dispatch()` — POST dispatches endpoint (204 → True)
- `head_sha` добавлен в return `get_latest_workflow_run()`
- `dotenv_builder.py` — `build_dotenv()` (sorted keys, quoting спецсимволов), `encode_dotenv()` (base64, warning >48KB)
- `deployed_sha` column в `service_deployments` (модель, схемы, роутер, миграция)
- `_setup_ci_secrets` → `_write_deploy_secrets` (DOTENV, DEPLOY_HOST, DEPLOY_USER, DEPLOY_SSH_KEY, DEPLOY_PORT, PROJECT_NAME)
- `DeployerNode.run()` переписан: build dotenv → write secrets → dispatch deploy.yml → wait → record с SHA → status active
- Race condition: `dispatch_time` записывается до trigger, передаётся как `created_after`
- Удалено: `devops_delegation.py`, `deployment_jobs.py`, `test_deploy_flow.py` placeholder
- 3 теста dispatch, 6 тестов dotenv, 8 тестов deployer
- E2E на живом стеке: API roundtrip, dotenv в контейнере, полный pipeline на GitHub (secrets → dispatch → wait → success)

#### Iteration 4 — Done (2026-02-16)

Реализовано:
- Удалён `services/infra-service/src/deployer/` (весь каталог)
- Из `services/infra-service/src/main.py` убран deploy handler, оставлен только `PROVISIONER_QUEUE`
- Удалён `ANSIBLE_DEPLOY_QUEUE` из `shared/queues.py` и topology
- infra-service теперь обслуживает только provisioning
- Все unit-тесты зелёные (272 passed), lint чистый

#### Iteration 5 — Done (2026-02-16)

Реализовано:
- `services/api/src/utils/webhook_security.py` — HMAC-SHA256 верификация подписи GitHub webhook
- `services/api/src/routers/webhooks.py` — `POST /webhooks/github` (без /api prefix)
  - Фильтрация: workflow_run + completed + success + ci.yml + main branch
  - Lookup проекта по `repository.id` → `project.github_repo_id`
  - Guard: project.status == active
  - Создание Task, публикация в deploy:queue
- `services/langgraph/src/workers/_events.py` — `publish_proactive_message()` для po:proactive
- `services/langgraph/src/workers/deploy_worker.py` — proactive notifications когда нет callback_stream
- `GITHUB_WEBHOOK_SECRET` добавлен в `.env.example` и `docker-compose.yml` (api service)
- 17 unit-тестов: 5 (webhook_security) + 8 (webhooks) + 4 (deploy_worker_proactive)
- Всего unit-тестов: 306 passed, lint чистый
- E2E на живом стеке:
  - curl: no signature → 401, bad signature → 401, push event → ignored, deploy.yml → ignored, unknown repo → ignored
  - Webhook → Task created → deploy:queue → deploy-worker → DevOps subgraph (env analysis, secret resolution, GitHub secrets) → 422 (ожидаемо: reverse-bot не имеет deploy.yml)
  - Guard для non-active проекта: повторный webhook во время deploying → корректно ignored
  - Proactive notification дошла в Telegram (после установки owner_id на проекте)

#### Iteration 6 — E2E выявил проблемы → Iterations 7-9

E2E тест выявил что CI `build-and-push` не работает: GHCR возвращает 403 при push образов.
Причина: GitHub App installation tokens не могут создавать пакеты в org namespace.
Подробный анализ: [docs/investigations/ghcr-403-app-token.md](./investigations/ghcr-403-app-token.md).

→ Решение: Iteration 7 (self-hosted Docker registry). После этого E2E-тесты выявили ещё две группы багов, зафиксированных в iterations 8-9.

#### Iteration 7 — Done (2026-02-16)

Реализовано:
- Caddy + Docker Registry добавлены в docker-compose (`caddy:2-alpine`, `registry:2`)
- `_compute_secret()`: `ghcr.io` → `ORCHESTRATOR_HOSTNAME` (self-hosted registry)
- `_write_deploy_secrets()`: +3 secrets (REGISTRY_URL, REGISTRY_USER, REGISTRY_PASSWORD)
- `infra/Caddyfile` создан (reverse proxy: /v2/* → registry, /webhooks/* → api)
- `.env.example` обновлён (ORCHESTRATOR_HOSTNAME, REGISTRY_USER, REGISTRY_PASSWORD, REGISTRY_PASSWORD_HASH)
- Итого unit-тестов: 128 (langgraph), +2 новых (image hostname raises, registry env missing)

#### Iteration 8 — Done (2026-02-17)

E2E тест `reverse-bot`: Developer вернул success без commit → вся pipeline продолжила без кода.
Подробный анализ: [docs/investigations/e2e-reverse-bot-cascade-failure.md](./investigations/e2e-reverse-bot-cascade-failure.md).

Реализовано (Level 2+3 cascade failure fixes):
- `developer.py`: валидация `commit_sha` — success без коммита → `blocked`
- `engineering_worker.py`: CI gate fail-closed (missing `repository_url` → `False`, не `True`)
- `engineering_worker.py`: deploy gate — `commit_sha=None` блокирует деплой
- `engineering_worker.py`: re-fetch project dict перед CI check (stale `repository_url` fix)
- `worker_wrapper/wrapper.py`: извлечение `commit_sha` из `git log` независимо от агента
- Worker sandbox: `site-packages` mount read-only (защита от Claude Code повреждения зависимостей)

#### Iteration 9 — Done (2026-02-17)

E2E тест `reverse-message-bot`: Registry secrets не настроены → CI fail → PO hallucination.
Подробный анализ: [docs/investigations/e2e-reverse-message-bot-registry-and-ci-bugs.md](./investigations/e2e-reverse-message-bot-registry-and-ci-bugs.md).

Реализовано:
- **BUG 1 (FIXED)**: Registry secrets chicken-and-egg — `_set_registry_secrets()` в scaffolder (до первого push)
- **BUG 3 (FIXED)**: CI poll `created_after` race condition — таймстамп инициализируется корректно
- **BUG 4 (FIXED)**: PO hallucination — 3-layer defense: event type в формате (`system_event:completed`), anti-hallucination промпт, `progress` события дропаются до LLM
- **BUG 5 (FIXED)**: Provisioner proxy fire-and-forget — убрано 20-мин ожидание, удалён мёртвый polling-код
- **BUG 2 (Backlog)**: CI fix agent misdiagnosis → задача для будущего CI Monitor Node

## Quick Links

- [Architecture](../ARCHITECTURE.md) — High-level system overview.
- [Testing Strategy](./TESTING.md) — How to run tests.
- [Contracts](./CONTRACTS.md) — Queue schemas and DTOs.
- [Audit](./audit.md) — Known issues and gaps.
