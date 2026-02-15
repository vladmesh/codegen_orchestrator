# Project Status

> **Current Focus**: PO ReactAgent Migration
> **Plan**: [po-react-agent.md](./plans/po-react-agent.md)

## PO ReactAgent Migration

Замена контейнерного PO (Docker + Claude CLI + orchestrator-cli) на LangGraph ReactAgent с прямым вызовом через Redis Streams.

### ✅ Completed

- **Phase 1**: PO Graph + Tools + Consumer (`services/langgraph/src/po/`)
- **Phase 1.5**: PostgreSQL Checkpointer (`AsyncPostgresSaver`, schema `langgraph`)
- **Phase 2.1**: Telegram Bot direct PO flow (`XADD po:input` → `XREAD po:response:{request_id}`)

### 🚧 Remaining

- [ ] Phase 2.2-2.5: System events, reminders, proactive messages
- [ ] Phase 3: Cleanup old container-based PO code

## 🔗 Quick Links

- [Architecture](../ARCHITECTURE.md) — High-level system overview.
- [Testing Strategy](./TESTING.md) — How to run tests.
- [Contracts](./CONTRACTS.md) — Queue schemas and DTOs.
- [Audit](./audit.md) — Known issues and gaps.
