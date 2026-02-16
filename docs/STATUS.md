# Project Status

> **Current Focus**: PO ReactAgent Migration
> **Plan**: [po-react-agent.md](./plans/po-react-agent.md)

## PO ReactAgent Migration

Замена контейнерного PO (Docker + Claude CLI + orchestrator-cli) на LangGraph ReactAgent с прямым вызовом через Redis Streams.

### Completed

- **Phase 1**: PO Graph + Tools + Consumer (`services/langgraph/src/po/`)
- **Phase 1.5**: PostgreSQL Checkpointer (`AsyncPostgresSaver`, schema `langgraph`)
- **Phase 2.1**: Telegram Bot direct PO flow (`XADD po:input` → `XREAD po:response:{request_id}`)
- **Phase 2.3**: System events through PO (`notify_user` tool, flat event format, `ProactiveListener`)
- **Phase 2.4**: Reminder poller (`_poll_once` + `run_reminder_poller`, `PO_REMINDERS_KEY` constant)
- **Phase 2.5**: Direct migration — `orchestrator respond` writes to `po:input` (no bridge needed)
- **Phase 3**: Cleanup — removed legacy container-based PO code (POSessionManager, PO worker contracts, PO state schema, dead tests, updated docs)

## Quick Links

- [Architecture](../ARCHITECTURE.md) — High-level system overview.
- [Testing Strategy](./TESTING.md) — How to run tests.
- [Contracts](./CONTRACTS.md) — Queue schemas and DTOs.
- [Audit](./audit.md) — Known issues and gaps.
