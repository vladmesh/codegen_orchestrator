# codegen_orchestrator stabilization plan v1

Version: 2026-07-23

This is the sequencing map for stabilizing codegen_orchestrator. The board remains the source of
truth for task ownership and detailed acceptance criteria; this plan only records the order, gates
and evidence links.

## Current position

Stages 1-7 are complete.

- Stages 1-4: the 2026-07-14 Sprint 002 closeout-audit rerun is GREEN on fetched `origin/main`
  `b0463fb34473c2756a40b669d2b7c5559b02d486` — details in [STATUS](../STATUS.md) and the
  [rerun report](../reports/sprint-002-closeout-audit-rerun.md).
- Stage 5 deterministic mock smoke landed in `codegen_orchestrator-496`; Stage 6 template
  compatibility matrix in `codegen_orchestrator-499` (one canonical harness reads the production
  ref from `scripts/system_configs.yaml` and accepts a candidate ref).
- Stage 7 closed with Mega 2.0 (`codegen_orchestrator-627`, PR #99: noop suite 7/7, live Claude
  path 5/5 through generated code, CI, merge, deploy, `/health` and non-LLM QA) plus the
  2026-07-18..21 hardening wave: fail-closed live teardown and quiescence fences (645, 659, 662),
  commit-exact `head_sha` env contracts and admin deploys (658, 661), title/slug split with the
  consumer sweep fix (646, 647, 663), teardown scripts extracted to
  `shared/live_harness_remote_cleanup.sh` (666), typed deploy outcomes (620), web port by role
  (665), the waiting_for_user_secret loop (670) and failure-safe task dispatch (672, PR #125).

Stage 8 (Telegram end-to-end) is next; its entry gate is satisfied. Remaining Stage 7 tail debt
is tracked on the external board and does not gate Stage 8: anonymous-volume cleanup (600),
lease-contract unification with the scaffolder (548), cleanup-layer extraction (676) followed by
manifest-driven recovery (527), the env_usage workflow-references audit (597), and the outbox
revision (673) which waits for live runs over the 672 dispatch fix.

## Production template rule

Production scaffolding uses GitHub as the only `service-template` source:
`gh:vladmesh/service-template`. The template version must be an explicit release tag, currently
`0.3.5`, stored in system config as `scheduler.service_template_ref`. Do not use `HEAD`, `main` or
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

- Closed 2026-07-21: the scaffolder 422 repo-collision hotfix candidate (backlog #1047) is
  obsoleted by `codegen_orchestrator-646` — repo names are `org/{project.slug}` with the full
  project UUID appended, so distinct projects cannot collide; see the closeout note in
  [backlog](../backlog.md).
- Stage 4 ride-along: `services/worker-manager/src/scaffold_phase.py` (unreferenced legacy scaffold
  path) removed with the Phase 3 dead-code slice (`codegen_orchestrator-457`).
- Deferred with triggers, recorded in the local [backlog](../backlog.md): event-driven task
  dispatcher (#1048), async deploy workflow wait (#1049), microVM worker runtime (#1050), elastic
  cloud-VM worker hosts (#1051).
- Done (`codegen_orchestrator-668`): the internal dogfooding machinery is gone. The orchestrator is
  no longer managed through itself; its tasks come from the external pipeline. The doc generators and
  their Makefile targets were removed, and `docs/backlog.md` / `docs/STATUS.md` are now maintained by
  hand. The Tasks/Stories API itself stays — it serves client projects.

## Stages

| Stage | Goal | Entry gate | Done when | References |
|---|---|---|---|---|
| 1. CI normalization | Make `main` mergeable only through a stable required CI gate. | Sprint 002 Phase 1 is active and `main` is blocked by formatting/CI drift. | Required CI Gate is the protected status check, format/lint/unit run unconditionally, and the CI contract checker is in place. | [STATUS](../STATUS.md), [CHANGELOG 2026-07-11](../CHANGELOG.md#2026-07-11), PR #30, `codegen_orchestrator-415` |
| 2. service-template contract audit | Replace guesses about generated-project behavior with a documented contract and deterministic smoke evidence. | CI normalization is complete enough to trust local checks. | Contract surface, current smoke result, closed findings and proposed follow-up cards are recorded without changing either repo. | [contract audit](../reports/codegen-service-template-contract.md), PR #31, `codegen_orchestrator-418` |
| 3. Small contract corrections | Fix narrow drift found by the audit before deeper hardening. | Audit findings have owner and evidence. | Worker-mode compose commands are proxied through `worker-start`/`worker-stop`; production scaffolding is pinned to GitHub `service-template` tag `0.3.0`; the local template mount is not part of production. | [contract audit §6.1-6.3](../reports/codegen-service-template-contract.md#61-makefile-patching-vs-a-native-orchestrator-api), [CHANGELOG 2026-07-12](../CHANGELOG.md#2026-07-12), PR #32, PR #33, `codegen_orchestrator-398`, `codegen_orchestrator-432` |
| 4. Sprint 002 phases 2-4 (COMPLETE, GREEN rerun) | Finish architectural hardening before autonomous recovery work. | CI and small service-template contract corrections are complete. | Response DTO enums, duplicated vocabularies, typed `RunResult`, typed Redis consume, dead-code removal and fail-fast conversions landed. The 2026-07-14 rerun closes all five Stage 4 blocker slices on fetched `origin/main`. | [original audit](../reports/sprint-002-closeout-audit.md), [rerun](../reports/sprint-002-closeout-audit-rerun.md), [Sprint 002](../sprints/002-thermo-nuclear-hardening/sprint.md), [thermo review](../thermo-nuclear-review.md) |
| 5. Deterministic mock smoke tests (COMPLETE) | Prove scaffold, worker-mode infra and generated app calls without live external services. | Sprint 002 phases 2-4 are complete or their touched contracts are stable enough for mock runs. | A repeatable mock smoke covers scaffold, setup, lint/tests, worker-start, smoke-probe, worker-call and cleanup with no resource leaks. | [contract audit §2](../reports/codegen-service-template-contract.md#2-deterministic-scaffold-smoke), `tests/integration/template/`, `tests/e2e/test_infrastructure_sanity.py` |
| 6. codegen + service-template matrix (COMPLETE) | Catch cross-repo drift before either side ships a breaking release. | Mock smoke is deterministic and the template tag bump process is documented. | Matrix runs current codegen against the pinned template tag and candidate template release, then records the resolved template commit. | [contract audit §4](../reports/codegen-service-template-contract.md#4-minimal-target-contract-v1), `scripts/system_configs.yaml`, `scheduler.service_template_ref` |
| 7. Live services (COMPLETE) | Validate provisioning, worker lifecycle, deploy and QA against real infrastructure after mock confidence. | Mock and matrix checks are green. | Live non-LLM services pass health, deploy and cleanup checks; failures produce typed outcomes rather than swallowed errors. | [DEV_PIPELINE](../DEV_PIPELINE.md), `make test-live`, `tests/live/` |
| 8. Telegram end-to-end | Put the user-facing Telegram path on top of the stabilized backend and live-service layers. | Live services are stable and Sprint 002 fail-fast work is complete. | Telegram request to deployed bot is verified last, with PO, scaffold, engineering, deploy and QA already covered underneath. | [CONTRACTS engineering flow](../CONTRACTS.md#engineering-flow), [ROADMAP stabilize core pipeline](../ROADMAP.md#current-arc-stabilize-core-pipeline) |
| 9. Worker isolation hardening | Remove platform credentials from worker containers so an agent inside a worker cannot read other tenants' data or push to other repos. | Telegram end-to-end is stable. Must land before onboarding external users. | Worker env no longer contains `REDIS_URL` or `SECRETS_ENCRYPTION_KEY`; the GitHub token is scoped per repo instead of org-wide; all worker egress goes through worker-manager proxy endpoints. | [swarm revision](../brainstorms/worker-db-network-isolation.md#ревизия-2026-07-12-гриллинг-роя), `services/worker-manager/src/manager.py:422-438` |
| 10. Swarm seams | Make worker capacity horizontal: adding a worker host becomes configuration, not a rewrite. | Worker isolation hardening is complete. Trigger: a second worker host or sustained parallel load. | Per-instance worker slot semaphore; hostname-based consumer name plus host field in worker status; ensure-workspace owned by worker-manager with `workspace_ready` meaning "scaffold pushed to git"; per-user concurrent slot cap; scaffolder no longer writes to the worker workspace path. | [swarm revision](../brainstorms/worker-db-network-isolation.md#ревизия-2026-07-12-гриллинг-роя), [scaling-15](../brainstorms/scaling-15-clients.md) |
