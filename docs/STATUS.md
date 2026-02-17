# Project Status

> **Current Focus**: Deploy Architecture (GitHub Actions + Fernet secrets + env groups)
> **Plan**: [deploy-architecture.md](./plans/deploy-architecture.md)

## Previous: PO ReactAgent Migration — Done

Plan: [po-react-agent.md](./plans/po-react-agent.md) — fully implemented and merged.

## Current: Deploy Architecture

E2E тест выявил что Ansible-деплой не работает. Переходим на GitHub Actions deploy + Fernet-шифрование секретов + env resolver pipeline.

6 итераций: Fernet crypto → Env groups → DeployerNode via GH Actions → Cleanup infra-service → Feature deploy flow → Final E2E.

### Status

- [x] Iteration 1: Fernet encryption для секретов
- [x] Iteration 2: Env Resolver с группами
- [x] Iteration 3: DeployerNode → GitHub Actions
- [x] Iteration 4: Очистка infra-service от deploy-кода
- [x] Iteration 5: Feature deploy via GitHub webhook
- [ ] Iteration 6: Финальный E2E и мёрж
- [x] **Iteration 7: Self-hosted Docker Registry + Caddy TLS**

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

#### Iteration 6 — Blocked

E2E тест выявил что CI `build-and-push` не работает: GHCR возвращает 403 при push образов.
Причина: GitHub App installation tokens не могут создавать пакеты в org namespace.
Подробный анализ: [docs/investigations/ghcr-403-app-token.md](./investigations/ghcr-403-app-token.md).

→ Решение: Iteration 7 (self-hosted Docker registry).

#### Iteration 7 — Done

Реализовано:
- Caddy + Docker Registry добавлены в docker-compose (`caddy:2-alpine`, `registry:2`)
- `_compute_secret()`: `ghcr.io` → `ORCHESTRATOR_HOSTNAME` (self-hosted registry)
- `_write_deploy_secrets()`: +3 secrets (REGISTRY_URL, REGISTRY_USER, REGISTRY_PASSWORD)
- `infra/Caddyfile` создан (reverse proxy: /v2/* → registry, /webhooks/* → api)
- `.env.example` обновлён (ORCHESTRATOR_HOSTNAME, REGISTRY_USER, REGISTRY_PASSWORD, REGISTRY_PASSWORD_HASH)
- Итого unit-тестов: 128 (langgraph), +2 новых (image hostname raises, registry env missing)

## Quick Links

- [Architecture](../ARCHITECTURE.md) — High-level system overview.
- [Testing Strategy](./TESTING.md) — How to run tests.
- [Contracts](./CONTRACTS.md) — Queue schemas and DTOs.
- [Audit](./audit.md) — Known issues and gaps.
