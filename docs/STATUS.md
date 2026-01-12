# Project Status

> **Current Phase**: Architecture Refactoring (Migration to 2.0)
> **Active Plan**: [MIGRATION_PLAN.md](./new_architecture/MIGRATION_PLAN.md)

## 🚀 Current Focus

**Phase 0: Foundation**

Закладываем фундамент: shared библиотеки и тестовая инфраструктура.

### Progress (Phase 0)

1. **✅ P0.1 Shared Contracts**
   - Пакет `shared/contracts` создан.
   - Все DTO и сообщения очередей реализованы.

2. **✅ P0.2 Shared Redis**
   - Пакет `shared/redis` создан.
   - Клиент поддерживает Pydantic DTO.
   - Добавлен `FakeRedisStreamClient` для тестов.

3. **✅ P0.3 Shared Logging**
   - Пакет `shared/logging` создан.
   - Настроен `structlog` (JSON/Console).
   - Поддержка `correlation_id`.

4. **✅ P0.4 GitHub Client**
   - GitHub App authentication (JWT).
   - Token caching и Rate Limiting.
   - Тесты с `respx` и `freezegun`.

5. **⏳ P0.5 Test Infrastructure** ← *NEXT*
   - [ ] Настроить 4-уровневую систему тестов (Unit/Service/Integration/E2E)
   - [ ] Перенести legacy тесты в карантин
   - [ ] Обновить Makefile и compose файлы

### Next Steps

**[P0.5 Test Infrastructure](./new_architecture/MIGRATION_PLAN.md#p05--test-infrastructure)**

После P0.5 начнём Phase 1 в TDD:
- **P1.0** — API Refactor (сначала тесты, потом код)

## 🔗 Quick Links

- [Migration Plan](./new_architecture/MIGRATION_PLAN.md)
- [Testing Strategy](./new_architecture/tests/TESTING_STRATEGY.md)
- [Legacy Backlog](./backlog.md)

