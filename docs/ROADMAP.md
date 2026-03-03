# Roadmap

## Phase 1: Stable Pipeline (scaffold -> code -> CI -> deploy)

Цель: надёжный конвейер от описания до работающего деплоя.

- [x] PO ReactAgent migration
- [x] Redis Streams unification (#3+#5)
- [x] Deploy architecture — Fernet, env groups, GH Actions, webhook, registry (9 iterations)
- [x] Dev environment — bind-mount, dual-network, compose proxy
- [x] Workspace persistence — project_id, git token refresh, PROGRESS.md, GC
- [x] Worker reuse for CI fix loop (#9)
- [x] Service template simplification (#1)
- [ ] Worker network isolation (#22) — in progress
- [ ] Fix & consolidate test suites (#6)

## Phase 2: Reliability & Self-Recovery

Цель: система восстанавливается сама, ошибки не теряются.

- [ ] Agent hierarchy & incident response (#2) — TaskAssessor, Watchdog, shared session memory
- [ ] Workspace failure counter & retry limit (#8)
- [ ] Deploy pre-check: validate server state (#21)
- [ ] Worker lifecycle: pause/unpause, CPU/RAM limits (#10)
- [ ] CI pipeline: parallel integration tests, branch protection (#4)

## Phase 3: Dev Process Automation

Цель: разработка самого оркестратора автоматизирована через скиллы.

- [ ] Dev pipeline skills: /next, /implement, /triage, /plan, /brainstorm, /checkpoint, /audit
- [ ] Self-maintaining docs: CHANGELOG, STATUS, ROADMAP auto-update
- [ ] Formalized backlog format (machine-readable Queue/Ideas/Done)

## Phase 4: MVP (closed beta)

Цель: первые пользователи. Telegram-боты генерируются, параллельность не ломается, всё видно, сложные запросы отклоняются.

- [ ] Admin UI & observability — воркеры, статусы, логи, трассировки LangGraph
- [ ] Parallel execution validation — state leaks, маршрутизация
- [ ] Assessor node — фильтр сложности запросов (вместо Architect на старте)
- [ ] Tester node (базовая версия) — валидация кода перед деплоем

## Phase 5: Capabilities Expansion

- [ ] Frontend battery — React/Vue/HTML generation (US6)
- [ ] Architect node — декомпозиция сложных задач
- [ ] "Add battery" to existing project — инкрементальное добавление модулей

## Phase 6: Scale

- [ ] Worker swarm — Docker-воркеры на отдельных VPS (10-20+ параллельных сборок)
- [ ] Cost tracking — LLM токены per user/project
- [ ] Self-hosted CI runner (или GitLab)
