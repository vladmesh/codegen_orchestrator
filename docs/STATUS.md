# Project Status

> **Current Focus**: Deploy Architecture (GitHub Actions + Fernet secrets + env groups)
> **Plan**: [deploy-architecture.md](./plans/deploy-architecture.md)

## Previous: PO ReactAgent Migration — Done

Plan: [po-react-agent.md](./plans/po-react-agent.md) — fully implemented and merged.

## Current: Deploy Architecture

E2E тест выявил что Ansible-деплой не работает. Переходим на GitHub Actions deploy + Fernet-шифрование секретов + env resolver pipeline.

6 итераций: Fernet crypto → Env groups → DeployerNode via GH Actions → Cleanup infra-service → Feature deploy flow → Final E2E.

### Status

- Starting Iteration 1: Fernet encryption для секретов

## Quick Links

- [Architecture](../ARCHITECTURE.md) — High-level system overview.
- [Testing Strategy](./TESTING.md) — How to run tests.
- [Contracts](./CONTRACTS.md) — Queue schemas and DTOs.
- [Audit](./audit.md) — Known issues and gaps.
