# codegen_orchestrator stabilization plan v1

Version: 2026-07-13

This is the sequencing map for stabilizing codegen_orchestrator. The board remains the source of
truth for task ownership and detailed acceptance criteria; this plan only records the order, gates
and evidence links.

## Current position

Stages 1-3 are complete. Stage 4 remains active after a RED Sprint 002 closeout audit:

- B7 response DTO enums are complete in PR #35 (`codegen_orchestrator-435`).
- Canonical contract vocabularies are complete in PR #36 (`codegen_orchestrator-436`). The final
  implementation preserves field-specific lifecycle subsets and terminates invalid provisioner
  queue entries instead of reclaiming them forever.
- Typed `RunResult` is complete in PR #38 (`codegen_orchestrator-440`), closing Phase 2.
- Phase 3 is complete: `consume_typed` (PR #40), typed worker result (PR #41), engineering consumer
  on `EngineeringMessage.model_validate` and dead-layer removal (`codegen_orchestrator-457`, PR #42). Raw
  `publish`/`publish_flat` stay public — ~13 live producers still call them; they migrate to
  `publish_message` per consumer over Phase 3/4 and the raw API was not extended.
- Phase 4 named slices landed. B3 infra incidents is complete in `codegen_orchestrator-466`: provisioning
  writes `READY` before incident-journal closure and scheduler reconciles only active provisioning
  failures for confirmed READY servers. B4 `secret_resolver` is complete in
  `codegen_orchestrator-473`: resolver inputs fail before deploy side effects and generated-secret
  persistence failures reach the deploy error path. The swallow-list and magic-number config
  conversions also landed, but the [closeout audit](../reports/sprint-002-closeout-audit.md) found
  live blockers in scaffolder shell/auth handling, worker compose false-success, bounded
  provisioner outage policy, notification caller policy and credential-safe diagnostics. Stage 4
  remains active; Stage 5 has not started.

## Production template rule

Production scaffolding uses GitHub as the only `service-template` source:
`gh:vladmesh/service-template`. The template version must be an explicit release tag, currently
`0.3.0`, stored in system config as `scheduler.service_template_ref`. Do not use `HEAD`, `main` or
another floating ref for production scaffolds. Bump the tag only after a contract/integration run
against the new template release.

## Swarm-readiness follow-ups (2026-07-12 grilling)

Decisions from the worker-swarm grilling live in the revision section of
[worker-db-network-isolation](../brainstorms/worker-db-network-isolation.md#ревизия-2026-07-12-гриллинг-роя).
Target model in one line: a worker stays an ephemeral Docker container; capacity scales by adding
hosts, each running a worker-manager replica that consumes the shared `worker:commands` stream. The
central `DOCKER_HOST=ssh://` control plane from the original brainstorm is dropped. Git is the
source of truth for workspaces; host workspace directories are caches.

Items that must not get lost:

- Hotfix candidate, before any second user: scaffolder swallows GitHub 422 on repo creation
  (`services/scaffolder/src/consumer.py:87-91`), so a project-name collision silently reuses and
  pushes into another project's repo. Fix: unique repo names (short project-id suffix) plus an
  ownership check when 422 still occurs. Tracked as backlog #1047.
- Stage 4 ride-along: `services/worker-manager/src/scaffold_phase.py` (unreferenced legacy scaffold
  path) removed with the Phase 3 dead-code slice (`codegen_orchestrator-457`).
- Deferred with triggers, recorded in the local [backlog](../backlog.md): event-driven task
  dispatcher (#1048), async deploy workflow wait (#1049), microVM worker runtime (#1050), elastic
  cloud-VM worker hosts (#1051).
- HIGH, backlog #1052: remove the internal dogfooding machinery entirely — the orchestrator is no
  longer managed through itself, its tasks come from the external pipeline. Kill the Makefile
  targets `backlog`/`roadmap`/`status`/`recent-artifacts`/`sync`/`task` and their generator
  scripts; they read the now-empty local Tasks DB and would wipe the hand-maintained
  `docs/backlog.md` and `docs/STATUS.md` if run. Update CLAUDE.md, DEV_PIPELINE.md and related
  workflow docs. The Tasks/Stories API itself stays — it serves client projects.

## Stages

| Stage | Goal | Entry gate | Done when | References |
|---|---|---|---|---|
| 1. CI normalization | Make `main` mergeable only through a stable required CI gate. | Sprint 002 Phase 1 is active and `main` is blocked by formatting/CI drift. | Required CI Gate is the protected status check, format/lint/unit run unconditionally, and the CI contract checker is in place. | [STATUS](../STATUS.md), [CHANGELOG 2026-07-11](../CHANGELOG.md#2026-07-11), PR #30, `codegen_orchestrator-415` |
| 2. service-template contract audit | Replace guesses about generated-project behavior with a documented contract and deterministic smoke evidence. | CI normalization is complete enough to trust local checks. | Contract surface, current smoke result, closed findings and proposed follow-up cards are recorded without changing either repo. | [contract audit](../reports/codegen-service-template-contract.md), PR #31, `codegen_orchestrator-418` |
| 3. Small contract corrections | Fix narrow drift found by the audit before deeper hardening. | Audit findings have owner and evidence. | Worker-mode compose commands are proxied through `worker-start`/`worker-stop`; production scaffolding is pinned to GitHub `service-template` tag `0.3.0`; the local template mount is not part of production. | [contract audit §6.1-6.3](../reports/codegen-service-template-contract.md#61-makefile-patching-vs-a-native-orchestrator-api), [CHANGELOG 2026-07-12](../CHANGELOG.md#2026-07-12), PR #32, PR #33, `codegen_orchestrator-398`, `codegen_orchestrator-432` |
| 4. Sprint 002 phases 2-4 (ACTIVE, RED audit) | Finish architectural hardening before autonomous recovery work. | CI and small service-template contract corrections are complete. | Response DTO enums, duplicated vocabularies, typed `RunResult`, typed Redis consume, dead-code removal and fail-fast conversions land phase by phase, then the closeout audit has no Stage 4 blockers. Phases 2/3 and most named Phase 4 slices are real; the 2026-07-13 audit is RED on five minimal blocker slices. | [closeout audit](../reports/sprint-002-closeout-audit.md), [Sprint 002](../sprints/002-thermo-nuclear-hardening/sprint.md), [thermo review](../thermo-nuclear-review.md), [ROADMAP autonomy](../ROADMAP.md#autonomy-smart-steward) |
| 5. Deterministic mock smoke tests | Prove scaffold, worker-mode infra and generated app calls without live external services. | Sprint 002 phases 2-4 are complete or their touched contracts are stable enough for mock runs. | A repeatable mock smoke covers scaffold, setup, lint/tests, worker-start, smoke-probe, worker-call and cleanup with no resource leaks. | [contract audit §2](../reports/codegen-service-template-contract.md#2-deterministic-scaffold-smoke), `tests/integration/template/`, `tests/e2e/test_infrastructure_sanity.py` |
| 6. codegen + service-template matrix | Catch cross-repo drift before either side ships a breaking release. | Mock smoke is deterministic and the template tag bump process is documented. | Matrix runs current codegen against the pinned template tag and candidate template release, then records the resolved template commit. | [contract audit §4](../reports/codegen-service-template-contract.md#4-minimal-target-contract-v1), `scripts/system_configs.yaml`, `scheduler.service_template_ref` |
| 7. Live services | Validate provisioning, worker lifecycle, deploy and QA against real infrastructure after mock confidence. | Mock and matrix checks are green. | Live non-LLM services pass health, deploy and cleanup checks; failures produce typed outcomes rather than swallowed errors. | [DEV_PIPELINE](../DEV_PIPELINE.md), `make test-live`, `tests/live/` |
| 8. Telegram end-to-end | Put the user-facing Telegram path on top of the stabilized backend and live-service layers. | Live services are stable and Sprint 002 fail-fast work is complete. | Telegram request to deployed bot is verified last, with PO, scaffold, engineering, deploy and QA already covered underneath. | [CONTRACTS engineering flow](../CONTRACTS.md#engineering-flow), [ROADMAP stabilize core pipeline](../ROADMAP.md#stabilize-core-pipeline) |
| 9. Worker isolation hardening | Remove platform credentials from worker containers so an agent inside a worker cannot read other tenants' data or push to other repos. | Telegram end-to-end is stable. Must land before onboarding external users. | Worker env no longer contains `REDIS_URL` or `SECRETS_ENCRYPTION_KEY`; the GitHub token is scoped per repo instead of org-wide; all worker egress goes through worker-manager proxy endpoints. | [swarm revision](../brainstorms/worker-db-network-isolation.md#ревизия-2026-07-12-гриллинг-роя), `services/worker-manager/src/manager.py:422-438` |
| 10. Swarm seams | Make worker capacity horizontal: adding a worker host becomes configuration, not a rewrite. | Worker isolation hardening is complete. Trigger: a second worker host or sustained parallel load. | Per-instance worker slot semaphore; hostname-based consumer name plus host field in worker status; ensure-workspace owned by worker-manager with `workspace_ready` meaning "scaffold pushed to git"; per-user concurrent slot cap; scaffolder no longer writes to the worker workspace path. | [swarm revision](../brainstorms/worker-db-network-isolation.md#ревизия-2026-07-12-гриллинг-роя), [scaling-15](../brainstorms/scaling-15-clients.md) |
