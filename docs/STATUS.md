# Sprint Status

Current stabilization map: [docs/plans/codegen-stabilization-v1.md](plans/codegen-stabilization-v1.md).

## Current Sprint
- **Sprint**: 002-thermo-nuclear-hardening
- **Goal**: Закрыть находки thermo-nuclear-review — типизированные границы, fail-fast, удаление мёртвого кода
- **Type**: tech
- **Started**: 2026-07-01
- **Current Phase**: Stage 5 next. Sprint 002 Phase 4 (silent failures → fail-fast) is complete
  after the [closeout audit rerun](reports/sprint-002-closeout-audit-rerun.md): all five prior
  blocker boundaries are closed on fetched `origin/main`; deterministic mock-smoke evidence is next.

## Current Facts

- CI normalization is complete in PR #30: `Required CI Gate`, unconditional format/lint/unit checks
  and `make ci-contract` are in place.
- service-template contract audit is complete in PR #31:
  [docs/reports/codegen-service-template-contract.md](reports/codegen-service-template-contract.md).
- `codegen_orchestrator-398` is complete: worker-mode compose proxy now targets
  service-template's `worker-start`/`worker-stop` in PR #32.
- `codegen_orchestrator-432` is the latest contract-correction layer in this baseline:
  production scaffolding uses GitHub `gh:vladmesh/service-template` with explicit tag `0.3.0`
  in PR #33.
- `codegen_orchestrator-435` is complete in PR #35: B7 response-DTO lifecycle fields use their
  `StrEnum` and reject unknown values at the read boundary.
- `codegen_orchestrator-436` is complete in PR #36: cross-service vocabularies are canonical,
  field-specific lifecycle wire subsets remain strict, and invalid provisioner results no longer
  poison-loop in the pending queue.
- `codegen_orchestrator-440` is complete in PR #38: `RunDTO.result` is now a
  per-`RunType` union (`EngineeringRunResult`/`DeployRunResult`/`QARunResult` in
  `shared/contracts/dto/run_result.py`) bound to `type`, not `dict | None`. Producers emit the typed
  model, the scheduler reads typed attributes, invalid results route to a visible terminal state.
  Closes the final slice of Phase 2.
- `codegen_orchestrator-457` is complete: closes Sprint 002 Phase 3. Engineering consumer now
  validates input via `EngineeringMessage.model_validate` before business logic (no more
  `job_data.get(...)` field unpacking or fallback defaults). Dead layers removed: legacy
  `services/langgraph/src/tools/` (projects/servers/github/specs + dead result models in
  `schemas/tools.py`) with the live `allocator` relocated to `services/langgraph/src/allocations.py`;
  the second `agent_config_cache`; the unreferenced `worker-manager/src/scaffold_phase.py`; the
  `worker:lifecycle` stream, its `WorkerLifecycleEvent` contract and `WorkerChannels.LIFECYCLE`
  member; and the `shared` compat-shims (`RedisStreamClient` try/except→None,
  `ServiceDeployment`/`DeploymentStatus` aliases, the legacy `DeploymentStatus` enum,
  `ensure_consumer_groups`). Raw `publish`/`publish_flat` were not privatized — ~13 live
  production producers still use them; that migration proceeds by consumer in Phase 3/4 and the raw
  API was not extended.
- `codegen_orchestrator-466` is complete: successful provisioning writes `READY` before attempting
  incident-journal closure. A temporary closure failure remains observable, sends one warning, and
  scheduler reconciliation later resolves only active `PROVISIONING_FAILED` incidents for confirmed
  `READY` servers without starting recovery work.
- Stage 6 template compatibility is implemented: one canonical Stage 5 harness reads the production
  source/ref from `scripts/system_configs.yaml`, accepts an explicit candidate ref, records the
  resolved commit SHA, isolates Compose resources and runs as independent non-fail-fast CI entries.

## Phase Progress
| Phase | Name | Status |
|-------|------|--------|
| 0 | Security quick-wins (B2 crypto, fail-open auth) | COMPLETE |
| 1 | Разблокировать CI (ruff format) + security-блокеры (B1, token-in-URL) | COMPLETE for CI normalization; remaining security items tracked by Sprint 002 |
| 2 | Затянуть контракты shared/ (B7 + словари + RunResult) | COMPLETE — B7 enums (`codegen_orchestrator-435`), duplicated vocabularies (`codegen_orchestrator-436`), typed `Run.result` union (`codegen_orchestrator-440`) |
| 3 | Типизированный consume + мёртвый код (B5, B6) | COMPLETE — `consume_typed` (PR #40), B6 worker result (PR #41), engineering consumer typed + dead-layer removal (`codegen_orchestrator-457`, PR #42) |
| 4 | Тихие ошибки → fail-fast (B3, B4, swallow-list) | COMPLETE — rerun audit on `b0463fb3` closes scaffolder/auth diagnostics, worker compose, provisioner outage and notification caller-policy boundaries. |

## Recent Stabilization Work

| Work | Status | Evidence |
|------|--------|----------|
| CI normalization | COMPLETE | PR #30, [CHANGELOG 2026-07-11](CHANGELOG.md#2026-07-11) |
| service-template contract audit | COMPLETE | PR #31, [contract audit](reports/codegen-service-template-contract.md) |
| worker-mode proxy target drift (`codegen_orchestrator-398`) | COMPLETE | PR #32, [CHANGELOG 2026-07-12](CHANGELOG.md#2026-07-12) |
| service-template production pin (`codegen_orchestrator-432`) | COMPLETE in current baseline | PR #33, `scheduler.service_template_ref=0.3.0` |
| Stabilization sequence (`codegen_orchestrator-434`) | COMPLETE | [stabilization plan v1](plans/codegen-stabilization-v1.md) |
| B7 response-DTO enums (`codegen_orchestrator-435`) | COMPLETE | lifecycle fields on task/story/server/application/incident/service-deployment DTOs now use their `StrEnum`; slice of Phase 2 only |
| Unified contract vocabularies (`codegen_orchestrator-436`) | COMPLETE | `shared/contracts/vocab.py` canonical `AgentType`/`ActionType`/`ResultStatus`/`LifecycleEvent`; inline `Literal` sets removed, `error` synonym dropped; `WorkerCliKind`/`DeployAction`/`TaskType` kept distinct; tests in `shared/tests/unit/test_vocab.py` |
| Typed `Run.result` union (`codegen_orchestrator-440`) | COMPLETE | `shared/contracts/dto/run_result.py` per-`RunType` models bound to `type`; producers emit typed models, scheduler reads typed attributes; invalid result → visible terminal state; tests in `shared/tests/unit/test_run_result.py` + `test_supervisor.py`. Closes Sprint 002 Phase 2 |
| Typed engineering consume + dead-layer removal (`codegen_orchestrator-457`) | COMPLETE | Engineering consumer on `EngineeringMessage.model_validate`; deleted `langgraph/src/tools/` (allocator → `allocations.py`), second `agent_config_cache`, `scaffold_phase.py`, `worker:lifecycle` stream+contract, shared compat-shims; tests in `test_engineering_validation.py`, `test_dead_layer_removed.py`, `test_phase3_shims_removed.py`. Closes Sprint 002 Phase 3. PR #42 |
| B3 incident journal reconciliation (`codegen_orchestrator-466`) | COMPLETE | Successful provisioning writes `READY` before journal closure. An unavailable journal remains observable and gets one warning; scheduler retries only active `provisioning_failed` entries for `READY` servers, idempotently and without recovery actions or per-tick notifications. |
| B4 secret resolver fail-fast (`codegen_orchestrator-473`) | COMPLETE | Resolver validates project context, allocations and repository metadata before deploy, rejects unknown computed values, and propagates secret-persistence failures through the deploy error path. The closeout audit tracks remaining Phase 4 boundaries separately. |
| Worker compose and provisioner outage bounds (`codegen_orchestrator-493`) | COMPLETE | Worker recipes preserve curl, JSON and compose failures and required override installation fails the task. Incident-journal reclaim retries only the journal write, then publishes one bounded terminal failure before ACK. |
| Sprint 002 closeout audit rerun (`codegen_orchestrator-495`) | COMPLETE, GREEN | [Original RED audit](reports/sprint-002-closeout-audit.md) is retained as historical evidence. The [rerun](reports/sprint-002-closeout-audit-rerun.md) verifies all five blocker slices on fetched `origin/main`; E2E remains pending and Stage 5 is next. |

## Sprint History

| # | Goal | Type | Dates | Phases |
|---|------|------|-------|--------|
| 001 | Tech code hygiene (github split, noqa, secret storage) | tech | 2026-04-10 – parked | Phase 0/1 done; Phase 2 поглощена 002 |
