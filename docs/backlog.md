# Backlog

> **Актуально на**: 2026-01-09

## Active Design & Implementation Plans

| Feature | Plan | Status |
|---------|------|--------|
| **Worker Lifecycle** | [worker-lifecycle.md](./tasks/worker-lifecycle.md) | **Planning** |
| **Orchestrator CLI Pydantic** | [orchestrator-cli-pydantic.md](./tasks/orchestrator-cli-pydantic.md) | Planning |
| **Secrets Vault** | [secrets-vault-implementation.md](./tasks/secrets-vault-implementation.md) | Design Ready |
| **GitHub Integration** | [github-worker-integration.md](./tasks/github-worker-integration.md) | Phase 1-3 Done |

---

### Agent Communication & Containers
**Приоритет**: HIGH
**Статус**: → Включено в [worker-lifecycle.md](./tasks/worker-lifecycle.md)

Объединено в Worker Lifecycle план:
- Два канала общения: JSON=остановился, API=продолжаю
- Container cleanup при shutdown
- Pause/unpause для экономии ресурсов
- Token tracking для session management

---

### TesterNode: Реальный запуск тестов
**Приоритет**: HIGH
**Статус**: TODO

TesterNode сейчас заглушка — всегда возвращает `passed=True`.

**Задачи:**
1. Использовать `worker_spawner` для запуска тестов в контейнере
2. Парсить результаты pytest
3. Возвращать ошибки для retry в Developer

---

### PO: Ожидание завершения deploy
**Приоритет**: MEDIUM  
**Статус**: TODO

PO не ждёт завершения деплоя — сразу отдаёт job_id пользователю.

**Решение**: Event-driven wake-up через Redis pub/sub.

---

### Caddy Reverse Proxy
**Приоритет**: MEDIUM  
**Статус**: TODO

Убрать port management, использовать Caddy для routing по доменам.

---

### Telegram Bot Pool
**Приоритет**: MEDIUM  
**Статус**: TODO

Пул pre-registered ботов для автоматического выделения проектам.

---

### API Authentication
**Приоритет**: MEDIUM  
**Статус**: TODO

API endpoints не защищены (только x-telegram-id header).

---

## Legacy Roadmap (from Old Status)

### Worker Lifecycle
- [ ] Модель коммуникации: JSON=остановился, API=продолжаю
- [ ] Pause/unpause контейнеров по timeout
- [ ] Cleanup при shutdown (решает проблему зависания)
- [ ] Token tracking из JSON output

### CLI Pydantic (агент ↔ система)
- [ ] Убрать curl/API из промптов

### Secrets (US1 requirement)
- [ ] GitHub Secrets как source of truth
- [ ] Метаданные в БД, значения в GitHub
- [ ] LLM не видит секреты

### E2E Integration
- [ ] E2E тест: US1 полный flow (токен → бот работает)

### Admin UI (Phase 2)
- [ ] UI для просмотра (Agents, Projects, Logos, Tokens)
- [ ] Конфигурация через UI (Prompts, Agent selection, TTL)
- [ ] Мониторинг (Grafana, Prometheus)

### Agent Architecture (Phase 3)
- [ ] Analyst Node (детализация требований)
- [ ] Engineering Lead (координация)
- [ ] Полноценный TesterNode
- [ ] Agent-to-agent communication

---

## Low Priority

### Docker Python SDK
Миграция workers-spawner с subprocess на Python Docker SDK.

### Rollback Capability
Откат к предыдущему деплою при failed health checks.

### Cost Tracking
Логирование tokens per request, агрегация по проектам.

### Human Escalation
Эскалация к человеку при застревании агента.

---

## Completed ✅

<details>
<summary>Выполненные задачи</summary>

- ✅ **Headless Mode Migration** (2026-01-08)
- ✅ **GitHub Capability** (2026-01-08)
- ✅ **Ralph-Wiggum Integration** (2026-01-08)
- ✅ **Deploy Worker Refactor** (2026-01-02)
- ✅ **CLI Agent Migration** — PO as pluggable CLI worker
- ✅ **Session Management** — Redis-based
- ✅ **Engineering Subgraph** — Architect → Preparer → Developer → Tester
- ✅ **DevOps Subgraph** — EnvAnalyzer → Deployer
- ✅ **Sysbox Installation** — Docker-in-Docker

</details>
