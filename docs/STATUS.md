# Sprint Status

Current stabilization map: [docs/plans/codegen-stabilization-v1.md](plans/codegen-stabilization-v1.md).

## Current Sprint
- **Sprint**: 002-thermo-nuclear-hardening
- **Goal**: Закрыть находки thermo-nuclear-review — типизированные границы, fail-fast, удаление мёртвого кода
- **Type**: tech
- **Started**: 2026-07-01
- **Current Phase**: Stabilization planning after completed CI and service-template contract work.
  Sprint 002 phases 2-4 remain pending until code changes prove otherwise.

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
- Current worker card: `codegen_orchestrator-435`, type B7 response-DTO lifecycle fields to their `StrEnum`
  (task/story/server/application/incident/service-deployment). This is a slice of Phase 2, not the whole phase.

## Phase Progress
| Phase | Name | Status |
|-------|------|--------|
| 0 | Security quick-wins (B2 crypto, fail-open auth) | COMPLETE |
| 1 | Разблокировать CI (ruff format) + security-блокеры (B1, token-in-URL) | COMPLETE for CI normalization; remaining security items tracked by Sprint 002 |
| 2 | Затянуть контракты shared/ (B7 + словари + RunResult) | In progress — B7 response-DTO enums done (`codegen_orchestrator-435`); словари и RunResult pending |
| 3 | Типизированный consume + мёртвый код (B5, B6) | Pending |
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

## Sprint History

| # | Goal | Type | Dates | Phases |
|---|------|------|-------|--------|
| 001 | Tech code hygiene (github split, noqa, secret storage) | tech | 2026-04-10 – parked | Phase 0/1 done; Phase 2 поглощена 002 |
