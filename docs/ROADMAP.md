# Roadmap

## Phase 1: Stable Pipeline (scaffold -> code -> CI -> deploy) — COMPLETE

Цель: надёжный конвейер от описания до работающего деплоя.

- [x] PO ReactAgent migration
- [x] Redis Streams unification (#3+#5)
- [x] Deploy architecture — Fernet, env groups, GH Actions, webhook, registry (9 iterations)
- [x] Dev environment — bind-mount, dual-network, compose proxy
- [x] Workspace persistence — project_id, git token refresh, PROGRESS.md, GC
- [x] Worker reuse for CI fix loop (#9)
- [x] Service template simplification (#1)
- [x] Worker network isolation (#22)
- [x] Fix & consolidate test suites (#6)

## Phase 2A: Pre-MVP (alpha blockers)

Цель: пустить 2-3 альфа-тестера. Изоляция, инфра, core product flow.

**Multi-user isolation:**
- [ ] Multi-user isolation fix — API auth bypass, worker ownership checks
- [ ] PO tools: pass user_id (#27) — проекты с owner, scoped list
- [ ] Port allocation locking — atomic allocate-or-fail

**Infrastructure (prod readiness):**
- [ ] Prod deploy pipeline — test deploy.yml, worker images build, DB backup cron
- [ ] Secrets hygiene — remove PEM from git, dedicated SSH key
- [x] Fix critical getenv defaults (#24)
- [x] Extract shared code (#23)
- [x] Post-deploy smoke tester (#25)
- [ ] Fix ORCHESTRATOR_USER_ID defaults (#29)

**Product:**
- [ ] US3: Add feature to existing project — PO tool + engineering flow + E2E

── ALPHA RELEASE ──

## Phase 2B: Post-alpha stability

Цель: по фидбеку альфы. Устойчивость, cleanup, оптимизация.

- [ ] Workspace failure counter & retry limit (#8)
- [ ] Deploy pre-check: validate server state (#21)
- [ ] Security: deploy cleanup (#7) — docker prune на серверах
- [ ] Worker lifecycle: pause/unpause, CPU/RAM limits (#10)
- [ ] Shared uv-cache isolation — per-project volume
- [ ] SOPS для .env на проде
- [ ] Фиксы по фидбеку альфа-тестеров

## Phase 3: Dev Process Automation & Task Store

Цель: автоматизация разработки + внутренняя "Jira" (dogfooding для продукта).

- [ ] Task Store в БД — Epic, WorkItem, WorkItemGate (dogfooding)
- [ ] API endpoints: /work-items, /epics
- [ ] Миграция скиллов на API-first (/next, /implement, /triage через API)
- [ ] Dev pipeline skills refinement
- [ ] CI pipeline: parallel integration tests, branch protection (#4)

## Phase 4: Public Beta

Цель: больше пользователей. Видимость, фильтрация, quality.

- [ ] Admin UI & observability — воркеры, статусы, логи, LangGraph traces
- [ ] Assessor node — фильтр сложности запросов
- [ ] Tester node (Claude + Playwright + Telethon) — AI-driven QA
- [ ] Agent hierarchy & incident response (#2) — Watchdog, Diagnostician

## Phase 5: Capabilities Expansion

- [ ] Frontend battery — React/Vue/HTML generation (US6)
- [ ] Architect node — декомпозиция сложных задач через Task Store
- [ ] "Add battery" to existing project — инкрементальное добавление модулей

## Phase 6: Scale

- [ ] Worker swarm — Docker-воркеры на отдельных VPS (10-20+ параллельных сборок)
- [ ] Cost tracking — LLM токены per user/project
- [ ] Self-hosted CI runner (или GitLab)
