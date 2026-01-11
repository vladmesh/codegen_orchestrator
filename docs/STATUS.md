# Project Status

> **Current Phase**: Architecture Refactoring (Migration to 2.0)
> **Active Plan**: [MIGRATION_PLAN.md](./new_architecture/MIGRATION_PLAN.md)

## 🚀 Current Focus

**Phase 0: Foundation**

Мы закладываем фундамент общей библиотеки `shared/`.
Реализованы базовые контракты, Redis клиент и логирование.

### Progress (Phase 0)

1. **✅ P0.1 Shared Contracts**
   - Пакет `shared/contracts` создан.
   - Все DTO и сообщения очередей реализованы.

2. **✅ P0.2 Shared Redis**
   - Пакет `shared/redis` создан.
   - Клиент поддерживает Pydantic DTO (`publish_message`).
   - Добавлен `FakeRedisStreamClient` для тестов.

3. **✅ P0.3 Shared Logging**
   - Пакет `shared/logging` создан.
   - Настроен `structlog` (JSON/Console).
   - Добавлена поддержка `correlation_id` (contextvars).

### Next Steps

**[P0.4 GitHub Client](./new_architecture/MIGRATION_PLAN.md#p04--github-client)**
- Реализация `shared/clients/github.py`.
- Поддержка GitHub App auth и Rate Limiting.

## 🔗 Quick Links

- [Migration Plan](./new_architecture/MIGRATION_PLAN.md)
- [Legacy Backlog](./backlog.md)
