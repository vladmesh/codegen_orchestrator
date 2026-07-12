# Sprint Status

Current stabilization map: [docs/plans/codegen-stabilization-v1.md](plans/codegen-stabilization-v1.md).

## Current Sprint
- **Sprint**: 002-thermo-nuclear-hardening
- **Goal**: Закрыть находки thermo-nuclear-review — типизированные границы, fail-fast, удаление мёртвого кода
- **Type**: tech
- **Started**: 2026-07-01
- **Current Phase**: Sprint 002 Phase 2 (shared contract tightening) is complete. Next is Phase 3
  (typed Redis consume + dead-code removal).

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

## Phase Progress
| Phase | Name | Status |
|-------|------|--------|
| 0 | Security quick-wins (B2 crypto, fail-open auth) | COMPLETE |
| 1 | Разблокировать CI (ruff format) + security-блокеры (B1, token-in-URL) | COMPLETE for CI normalization; remaining security items tracked by Sprint 002 |
| 2 | Затянуть контракты shared/ (B7 + словари + RunResult) | COMPLETE — B7 enums (`codegen_orchestrator-435`), duplicated vocabularies (`codegen_orchestrator-436`), typed `Run.result` union (`codegen_orchestrator-440`) |
| 3 | Типизированный consume + мёртвый код (B5, B6) | Pending — next |
| 4 | Тихие ошибки → fail-fast (B3, B4, swallow-list) | Pending |

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

## Sprint History

| # | Goal | Type | Dates | Phases |
|---|------|------|-------|--------|
| 001 | Tech code hygiene (github split, noqa, secret storage) | tech | 2026-04-10 – parked | Phase 0/1 done; Phase 2 поглощена 002 |
