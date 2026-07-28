# Backlog (Deferred Pool)

> This is NOT a work queue. Active work lives in sprint task files (`docs/sprints/`).
> Backlog holds deferred items: tech debt, ideas, future work. Processed during tech sprints (every 5th sprint or when >30 items).
> Purely local, hand-maintained file: the internal Tasks pipeline is inactive, orchestrator tasks go
> through the external pipeline.

## Queue

### #1022 API authorization: scope worker access, protect destructive endpoints
- **Priority**: HIGH (multi-tenant hardening arc)
- **Plan**: —
- **Status**: backlog
- **Brainstorm**: [api-visibility-scoping.md](brainstorms/api-visibility-scoping.md)
- **Brief**: The API is almost entirely open — no auth on tasks, stories, projects endpoints. Servers/allocations have optional admin check that skips if no header sent. Currently safe only because API listens on localhost and Caddy only proxies /webhooks/* and /v2/*. But inside the Docker network any contain...

### #1005 Standardize PYTHONPATH and import patterns across service-template services
- **Priority**: LOW
- **Plan**: —
- **Status**: backlog
- **Brief**: Currently tg_bot uses PYTHONPATH=/app:/app/services/tg_bot/src (allowing relative imports) while backend and notifications_worker use PYTHONPATH=/app (requiring fully qualified imports like services.backend.src.module). This inconsistency causes coding agents to guess wrong import patterns, leadi...


### #1017 Container drift detection via cadvisor (orphans/ghosts in health_checker)
- **Priority**: LOW
- **Plan**: —
- **Status**: backlog
- **Brief**: In health_checker: compare cadvisor container list with applications from API. Orphan (on server, not in DB) → warning in admin UI on server card. Ghost (RUNNING in DB, no container) → update status to DOWN + warning in admin UI. Source: brainstorm bs-69482380, Phase 3.

### #1018 Daily SSH job: filesystem drift check + docker prune
- **Priority**: LOW
- **Plan**: —
- **Status**: backlog
- **Brief**: Single SSH connection per server, once daily. Check /opt/projects/ vs API — orphan dirs → warning in structlog (NOT admin UI). Docker system prune -af --filter until=72h + docker volume prune -f. Source: brainstorm bs-69482380, Phase 3.

### #7 Security Audit: Deploy Cleanup
- **Priority**: LOW
- **Plan**: yes (in work item)
- **Status**: backlog
- **Brief**: cleanup of leftover containers/images after deploys (`docker image prune`). SSH hardening is already done in ansible. Priority adjusted by triage (roadmap phase change).

### #10 Worker Lifecycle (Pause/Unpause)
- **Priority**: HIGH (multi-tenant hardening arc: isolation and resource limits)
- **Plan**: —
- **Status**: backlog
- **Brief**: `docker pause` when idle. CPU/RAM limits on the containers.

### #1024 Integrate Repository into production flows (webhook, scheduler, worker)
- **Priority**: LOW
- **Plan**: —
- **Status**: backlog
- **Brief**: wire the Repository model into the production pipeline. Right now webhook/scheduler/worker use Project.repository_url and Project.github_repo_id directly.  1. webhooks.py: look up through Repository.provider_repo_id instead of Project.github_repo_id 2. github_sync.py: creates Repository records instead of updat...

### #1025 Fix eager import chains in scaffolded projects

- **Priority**: LOW
- **Plan**: —
- **Status**: backlog
- **Brief**: __init__.py eagerly imports app/create_app, which triggers full import chain. Any broken import crashes everything including alembic. Fix: lazy imports or direct model import in env.py.

### #1026 Auto-generate routers from domain specs

- **Priority**: LOW
- **Plan**: —
- **Status**: backlog
- **Brief**: Framework generates protocols and controller stubs but routers are manual. Router pattern is formulaic — generate stubs to reduce boilerplate and prevent spec drift.

### #1027 Add predefined module to existing project (make add-module)

- **Priority**: LOW
- **Plan**: —
- **Status**: backlog
- **Brief**: Allow adding tg_bot/notifications/frontend to a project generated without them. Currently requires re-generation.

### #2 Agent Hierarchy & Incident Response
- **Priority**: LOW
- **Plan**: —
- **Status**: backlog
- **Brief**: TaskAssessor, Watchdog & Recovery (DockerEventsListener, DLQ consumer), shared session memory (the agent's "dying note"). Brainstorm: `docs/brainstorms/agent-hierarchy.md`. Priority adjusted by triage (roadmap phase change). NB: the Watchdog/DLQ scope will shrink — WorkItemEvent (#55) covers a...

### #20 API Key & SSH Key Encryption
- **Priority**: LOW
- **Plan**: —
- **Status**: backlog
- **Brief**: apply SecretsCipher (Fernet) to the API key values and the SSH keys. TODO comments in `api_keys.py:36,72` and `servers.py:66`.

### #1028 Unified handlers: error handling strategy

- **Priority**: LOW
- **Plan**: —
- **Status**: backlog
- **Brief**: Define error handling for event handlers: DLQ, error events, or retries with exponential backoff.

### #1029 Auto-update __init__.py re-exports after generation

- **Priority**: LOW
- **Plan**: —
- **Status**: backlog
- **Brief**: After adding new models, schemas/__init__.py etc must be manually updated. Generate these files or remove re-export pattern.

### #1030 Context packer for agents (make context service=backend)

- **Priority**: LOW
- **Plan**: —
- **Status**: backlog
- **Brief**: Aggregate relevant spec, AGENTS.md, signatures, linter errors into single token-optimized file for agent context.

### #11 E2E Tests Completion
- **Priority**: LOW
- **Plan**: —
- **Status**: backlog
- **Brief**: finish the E2E coverage (Level 5-7). Add E2E mock tests (Level A+B) to CI.

### #26 Notifications via Redis Stream (remove the direct dependency on the Telegram API)
- **Priority**: LOW
- **Plan**: —
- **Status**: backlog
- **Brief**: right now `shared/notifications.py` sends to the Telegram API directly — scheduler and infra-service hold `TELEGRAM_BOT_TOKEN`. What is needed: the services publish to the Redis stream `notifications:queue`, telegram_bot consumes and sends. Removes `TELEGRAM_BOT_TOKEN` from every service except telegram_bot, simplifies tes...

### #41 Parallel Server Provisioning
- **Priority**: HIGH (multi-tenant hardening arc)
- **Plan**: —
- **Status**: backlog
- **Brief**: infra-service processes `provisioner:queue` sequentially — a single consumer loop with an `await` on every job (`services/infra-service/src/main.py:127-148`). With 3+ servers in `PENDING_SETUP` every Ansible run (~15 min) blocks the queue. The LangGraph side is already parallel (`asyncio.create_task`...

### #1031 Auto-fuzzing and contract testing (schemathesis)

- **Priority**: LOW
- **Plan**: —
- **Status**: backlog
- **Brief**: Integrate schemathesis into CI. Reads openapi.json, fuzzes running service with valid/invalid inputs. Auto-detect 500 errors without manual tests.

### #1032 Extract type mappings into language-agnostic config

- **Priority**: LOW
- **Plan**: —
- **Status**: backlog
- **Brief**: Partially done: type_spec_to_python() centralized in spec/types.py. Remaining: unify all mappings (Python, TypeScript, OpenAPI) via single table/config. Extract to YAML/TOML for adding new languages without code.

### #1033 Enum types in model field definitions

- **Priority**: LOW
- **Plan**: —
- **Status**: backlog
- **Brief**: Support enum types in YAML specs. Generated Pydantic models would use Literal or Enum instead of plain strings.

### #1034 CLI wrappers (my-framework init/sync/update)

- **Priority**: LOW
- **Plan**: —
- **Status**: backlog
- **Brief**: Wrap make commands into standalone CLI tool. Simplify usage for humans and agents.

### #1035 Celery worker support

- **Priority**: LOW
- **Plan**: —
- **Status**: backlog
- **Brief**: Add celery-worker service type. Pre-configured Redis/RabbitMQ in docker-compose, auto-generated celery_app and task decorators.

### #1036 Audit scaffold templates for best practices

- **Priority**: LOW
- **Plan**: —
- **Status**: backlog
- **Brief**: Review templates in .framework/framework/templates/scaffold/services/ to ensure they use latest patterns adopted by main services.

### #1037 Unified handlers: transactional outbox pattern

- **Priority**: LOW
- **Plan**: —
- **Status**: backlog
- **Brief**: Events published directly after DB writes. Consider transactional outbox to avoid dual write problem.

### #1038 High-level architecture spec (connectivity graph)

- **Priority**: LOW
- **Plan**: —
- **Status**: backlog
- **Brief**: Define service relationships in services.yml: access, exposes, consumes. Generate typed clients and network policies.

### #1039 Spec-first observability (auto OpenTelemetry)

- **Priority**: LOW
- **Plan**: —
- **Status**: backlog
- **Brief**: Auto-embed traces and metrics into generated endpoints. Zero-config observability from spec definitions.

### #1040 Make YAML specs fully language-agnostic

- **Priority**: LOW
- **Plan**: —
- **Status**: backlog
- **Brief**: Partially done: abstract types used. Remaining: replace list[string] shorthand with JSON Schema array+items for full language-agnosticity.

### #1041 Spec-only module storage (long-term)

- **Priority**: LOW
- **Plan**: —
- **Status**: backlog
- **Brief**: Store only specs and minimal scaffolds, generate all business logic on project creation. Zero distinction between built-in and custom services.

### #1042 Rust PoC: backend service on Axum

- **Priority**: LOW
- **Plan**: —
- **Status**: backlog
- **Brief**: Proof of concept — Axum + SeaORM 2.0 + utoipa. Same API, same Docker, same compose as Python backend. Test how well AI agent handles Axum code generation.

### #1043 Rust PoC: Telegram bot on teloxide

- **Priority**: LOW
- **Plan**: —
- **Status**: backlog
- **Brief**: PoC Telegram bot on teloxide as alternative to python-telegram-bot. Compare developer and agent experience.

### #1044 Research Tera as Jinja2 replacement for codegen

- **Priority**: LOW
- **Plan**: —
- **Status**: backlog
- **Brief**: Tera is Rust Jinja2 analog with near-identical syntax. Evaluate how many current templates can be reused. If 90%+ compatible, migration cost is low.

### #1045 Add Rust service type to services.yml

- **Priority**: LOW
- **Plan**: —
- **Status**: backlog
- **Brief**: New rust-axum service type. Scaffold template with Cargo.toml, multi-stage Dockerfile (cargo-chef), main.rs. Enables mixing Python and Rust services.

### #1003 Integration test: scheduler-langgraph story worker lifecycle
- **Priority**: LOW
- **Plan**: —
- **Status**: backlog
- **Brief**: Create integration test compose (scheduler + langgraph + Redis) that verifies the cross-service story worker flow: dispatcher sends story_id in EngineeringMessage -> consumer reads it, spawns worker, stores in registry -> dispatcher cleanup on story complete removes worker.

### #1046 Allocate ports only for modules that need host exposure
- **Priority**: LOW
- **Plan**: —
- **Status**: backlog
- **Brief**: Currently ensure_project_allocations allocates a port for every module in the list (e.g. backend + tg_bot). But tg_bot does not listen on a host port — it connects outbound to Telegram API. Allocating a port for it wastes the resource and clutters the admin UI.  Fix: modules should declare whethe...

### #1047 [hotfix candidate] Repo creation: stop swallowing GitHub 422, ensure unique repo names
- **Priority**: —
- **Plan**: —
- **Status**: closed (obsoleted by codegen_orchestrator-646)
- **Brainstorm**: [scaling-15-clients.md](brainstorms/scaling-15-clients.md)
- **Brief**: scaffolder ignores 422 "repository already exists" on create_repo (services/scaffolder/src/consumer.py:87-91). Meant as idempotent retry, but a project-name collision between two projects silently reuses and pushes into the other project's repo. Fix: suffix repo names with a short project-id hash + after a 422 verify the repo belongs to this project, else fail fast.
- **Closeout (2026-07-21)**: The collision premise is structurally impossible since 646. The repo name is `org/{project.slug}`, and `generate_project_slug` (`shared/project_slug.py`) appends the full `project_id.hex`, so two distinct projects can never share a repo name even with identical titles. That is the exact fix this item proposed (project-id suffix), delivered stronger (full UUID, not a short hash). The remaining 422 swallow now fires only on the same project's own scaffold retry (redelivery / crash-recovery), where reusing that project's own repo is correct. The proposed ownership check on 422 would only guard a manually pre-created `org/{base}-{that-exact-uuid}` repo, which is not a real threat. Closed without code change.

### #1048 Event-driven task dispatcher (replace 30s polling)
- **Priority**: LOW (trigger: first signs of parallel load)
- **Plan**: —
- **Status**: backlog
- **Brainstorm**: [scaling-15-clients.md](brainstorms/scaling-15-clients.md)
- **Brief**: Task dispatcher polls the DB every scheduler.dispatch_interval_seconds. Under load this turns the pipeline into batch sessions. React to task/story status events instead of polling, or shorten the interval as a stopgap.

### #1049 Async wait for deploy workflow completion
- **Priority**: LOW (trigger: parallel deploys)
- **Plan**: —
- **Status**: backlog
- **Brainstorm**: [scaling-15-clients.md](brainstorms/scaling-15-clients.md)
- **Brief**: Deployer waits for deploy.yml via API polling with a 600s timeout. Parallel deploys hold worker slots for the whole wait. Make the wait non-blocking (poll task queue / webhook) so a deploy in progress does not occupy a slot.

### #1050 MicroVM worker runtime (Kata Containers / Firecracker)
- **Priority**: LOW (trigger: untrusted external users need hard isolation)
- **Plan**: —
- **Status**: backlog
- **Brainstorm**: [worker-db-network-isolation.md](brainstorms/worker-db-network-isolation.md)
- **Brief**: Worker stays a container from worker-manager's point of view; Kata runtime transparently turns docker run into a microVM (kernel-level isolation, boot <125ms). Per-host opt-in, no model change. Requires bare-metal/KVM hosts. Complements stabilization Stage 9 (credential scoping), does not replace it.

### #1051 Elastic worker hosts (cloud VMs on-demand)
- **Priority**: LOW (trigger: worker farm saturation; decide on real load data)
- **Plan**: —
- **Status**: backlog
- **Brainstorm**: [worker-db-network-isolation.md](brainstorms/worker-db-network-isolation.md)
- **Brief**: With per-host worker-manager replicas (stabilization Stage 10), an elastic host is just a cloud VM that boots, starts a worker-manager replica pointed at the shared Redis, drains the queue and dies. Hetzner Cloud API + cloud-init, optional pre-warm pool. Costed in the brainstorm at ~€1-3/day for 10 workers.
