# Workers-Spawner Implementation Plan

План реализации унифицированного сервиса управления агентами.

## Phase 1: Orchestrator CLI Refactoring
**Цель:** Подготовить универсальный инструмент для агентов с поддержкой прав доступа.

1.  **Extract Service:**
    *   Move `services/agent-worker/cli` -> `services/orchestrator-cli`.
    *   Update `pyproject.toml` and structure.

2.  **Permission System:**
    *   Implement `PermissionManager` in CLI.
    *   Read `ORCHESTRATOR_ALLOWED_TOOLS` env var.
    *   Decorate commands to check permissions before execution.

3.  **Refine Commands:**
    *   Ensure all necessary tools (Project, Deploy, Respond) are implemented.
    *   Add `--json` output support for all commands (critical for robotic agents).

## Phase 2: Workers-Spawner Service
**Цель:** Создать сервис-оркестратор контейнеров.

1.  **Service Skeleton:**
    *   Create `services/workers-spawner` structure.
    *   Implement Redis Stream listener (`workers:spawn`).

2.  **Configuration & Factory:**
    *   Implement `WorkerConfig` usage (JSON loading).
    *   Implement `SkillGenerator`:
        *   Takes list of `allowed_tools`.
        *   Reads markdown templates (`deploy.md`, `project.md`...).
        *   Generates `CLAUDE.md`.

3.  **Docker Integration:**
    *   Implement `ContainerManager`.
    *   Logic for `docker run`:
        *   Env vars injection (`ORCHESTRATOR_ALLOWED_TOOLS`).
        *   Volume mounting for skills/config.
        *   Bootstrap command generation.

4.  **Discovery:**
    *   Define default presets (`presets.json` instead of Python code).
    *   Supported presets: `po_claude`, `developer_droid`, `developer_claude`.

## Phase 3: Universal Worker Image
**Цель:** Единый Docker-образ.

1.  **Dockerfile:**
    *   Base: Ubuntu + Python + Node.
    *   Pre-install `orchestrator-cli`.
    *   Include `bootstrap.sh`.

2.  **Bootstrap Script:**
    *   Logic to install extra packages at runtime.
    *   Logic to setup Agent-specific configs (Claude vs Droid).

## Phase 4: Migration

1.  **Migrate Developer Droid:**
    *   Switch `langgraph` node "Developer" to publish to `workers:spawn`.
    *   Verify Droid works via new spawner.

2.  **Migrate Product Owner:**
    *   Switch Telegram Bot to publish to `workers:spawn`.
    *   Verify PO works via new spawner.

3.  **Enable Developer Claude:**
    *   Add `developer_claude` preset.
    *   Test engineering task with Claude Agent.

## Phase 5: Cleanup

1.  Remove `services/agent-spawner`.
2.  Remove `services/worker-spawner`.
3.  Remove legacy `Tool` classes in LangGraph (if any remain).

## Timeline Estimate

| Phase | Effort |
|-------|--------|
| 1. Orchestrator CLI | 1 day |
| 2. Spawner Service | 2 days |
| 3. Universal Image | 0.5 day |
| 4. Migration | 1-2 days |
| 5. Cleanup | 0.5 day |
| **Total** | **~1 week** |
