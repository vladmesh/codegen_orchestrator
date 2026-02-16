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
- [ ] Iteration 2: Env Resolver с группами
- [ ] Iteration 3: DeployerNode → GitHub Actions
- [ ] Iteration 4: Очистка infra-service от deploy-кода
- [ ] Iteration 5: Feature deploy flow
- [ ] Iteration 6: Финальный E2E и мёрж

#### Iteration 1 — Done (2026-02-16)

Реализовано:
- `shared/crypto.py` — `SecretsCipher` (Fernet), `encrypt_dict`/`decrypt_dict`
- Graceful degradation: plaintext значения проходят через decrypt без ошибок (warning в лог)
- Encrypt-on-write миграция: при первой записи все старые plaintext мигрируют в encrypted
- Интеграция в 3 точках: `SecretResolverNode`, `set_project_secret` (PO tool), `set_secret_async` (CLI)
- `SECRETS_ENCRYPTION_KEY` проброшен в docker-compose (4 сервиса) + worker containers
- 10 unit-тестов для crypto, 2 для SecretResolver encryption, 1 для PO tools encryption
- E2E проверен на живом стеке: все секреты `reverse-bot` мигрировали в `gAAAAA...`

## Quick Links

- [Architecture](../ARCHITECTURE.md) — High-level system overview.
- [Testing Strategy](./TESTING.md) — How to run tests.
- [Contracts](./CONTRACTS.md) — Queue schemas and DTOs.
- [Audit](./audit.md) — Known issues and gaps.
