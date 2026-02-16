# Backlog

> **Актуально на**: 2026-02-14

## Active Design & Implementation Plans

| Feature | Plan | Status |
|---------|------|--------|
| **Worker Lifecycle** | [worker-lifecycle.md](./tasks/worker-lifecycle.md) | Planning (нужна переработка под worker-manager) |
| **Secrets Vault** | [secrets-vault-implementation.md](./tasks/secrets-vault-implementation.md) | Design Ready |

---

## 🔴 HIGH Priority

### TesterNode: Реальный запуск тестов
**Статус**: TODO

TesterNode сейчас заглушка — всегда возвращает `passed=True`.

**Задачи:**
1. Использовать `worker-manager` для запуска тестов в контейнере
2. Парсить результаты pytest
3. Возвращать ошибки для retry в Developer

---

### API Authentication
**Статус**: TODO

API endpoints не защищены (только x-telegram-id header).
Любой с доступом к сети может вызывать API.

**Решение**: API key / JWT аутентификация.

---

### Docker Events Listener: Обновление статуса воркеров
**Статус**: TODO

`DockerEventsListener` (worker-manager/src/events.py) — заглушка, не слушает Docker-события.
Когда контейнер воркера умирает (kill, crash, restart), `worker:status:{id}` в Redis остаётся `RUNNING`.
Telegram-бот видит `RUNNING` → шлёт сообщения в стрим мёртвого контейнера → таймаут, пользователь не получает ответ.

**Задачи:**
1. Реализовать подписку на Docker events (`container die/stop/destroy`) через Docker SDK или API
2. При `die`/`stop` — обновлять `worker:status:{id}` → `STOPPED` в Redis
3. Опционально: уведомлять пользователя через callback stream что воркер упал

**Связано с**: Worker Lifecycle (pause/unpause, cleanup)

---

### Resource Limits (Worker Manager)
**Статус**: TODO

Нет ограничений на ресурсы:
- `MAX_CONCURRENT_WORKERS` — количество одновременных контейнеров
- Memory/CPU limits на контейнеры
- Disk usage limits

**Влияние**: Один пользователь может исчерпать все ресурсы хоста.

---

### Admin UI
**Статус**: TODO

Без админки невозможно нормально отлаживать проект. Нужна хотя бы базовая версия.

**Базовая версия:**
- Просмотр: Projects, Workers, Logs
- Мониторинг состояния системы

**Полная версия (позже):**
- Конфигурация через UI (Prompts, Agent selection, TTL)
- Мониторинг (Grafana, Prometheus)

---

## 🟡 MEDIUM Priority

### Worker Lifecycle (Pause/Unpause, Cleanup, Token Tracking)
**Статус**: TODO — план в [worker-lifecycle.md](./tasks/worker-lifecycle.md), требует переработки

**Задачи:**
1. Idle pause/wakeup — `docker pause/unpause` по таймауту неактивности
2. Container cleanup при shutdown
3. Token tracking из Claude Code JSON output
4. Creation queue — очередь создания воркеров с приоритетами

---

### E2E тесты
**Статус**: Фазы 1-4 готовы, фазы 5-7 не реализованы

Завершить E2E покрытие. Full system docker-compose validation.

---

### Secrets Vault
**Статус**: Design Ready — план в [secrets-vault-implementation.md](./tasks/secrets-vault-implementation.md)

- GitHub Secrets как source of truth
- Метаданные в БД, значения в GitHub
- LLM не видит секреты

---

### Caddy Reverse Proxy
**Статус**: TODO

Убрать port management, использовать Caddy для routing по доменам.

---

## 🟢 LOW Priority

### Telegram Bot Pool
Пул pre-registered ботов для автоматического выделения проектам.

### Docker Python SDK
Миграция worker-manager с subprocess на Python Docker SDK.

### Rollback Capability
Откат к предыдущему деплою при failed health checks.

### Cost Tracking
Логирование tokens per request, агрегация по проектам.

### Human Escalation
Эскалация к человеку при застревании агента.

---

## 📦 Phase 3 (Future)

### Agent Architecture
- Engineering Lead (координация)
- Agent-to-agent communication
