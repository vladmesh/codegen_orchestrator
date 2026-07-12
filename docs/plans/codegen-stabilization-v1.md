# codegen_orchestrator stabilization plan v1

Version: 2026-07-12

This is the sequencing map for stabilizing codegen_orchestrator. The board remains the source of
truth for task ownership and detailed acceptance criteria; this plan only records the order, gates
and evidence links.

## Current position

Stages 1-3 are complete. Stage 4 is active at Sprint 002 Phase 2:

- B7 response DTO enums are complete in PR #35 (`codegen_orchestrator-435`).
- Canonical contract vocabularies are complete in PR #36 (`codegen_orchestrator-436`). The final
  implementation preserves field-specific lifecycle subsets and terminates invalid provisioner
  queue entries instead of reclaiming them forever.
- Next is typed `RunResult`, the last Phase 2 slice. Phase 3 then adds typed Redis consume, migrates
  worker/engineering consumers and removes dead layers. Phase 4 converts the remaining silent
  failures to fail-fast behavior.

## Production template rule

Production scaffolding uses GitHub as the only `service-template` source:
`gh:vladmesh/service-template`. The template version must be an explicit release tag, currently
`0.3.0`, stored in system config as `scheduler.service_template_ref`. Do not use `HEAD`, `main` or
another floating ref for production scaffolds. Bump the tag only after a contract/integration run
against the new template release.

## Stages

| Stage | Goal | Entry gate | Done when | References |
|---|---|---|---|---|
| 1. CI normalization | Make `main` mergeable only through a stable required CI gate. | Sprint 002 Phase 1 is active and `main` is blocked by formatting/CI drift. | Required CI Gate is the protected status check, format/lint/unit run unconditionally, and the CI contract checker is in place. | [STATUS](../STATUS.md), [CHANGELOG 2026-07-11](../CHANGELOG.md#2026-07-11), PR #30, `codegen_orchestrator-415` |
| 2. service-template contract audit | Replace guesses about generated-project behavior with a documented contract and deterministic smoke evidence. | CI normalization is complete enough to trust local checks. | Contract surface, current smoke result, closed findings and proposed follow-up cards are recorded without changing either repo. | [contract audit](../reports/codegen-service-template-contract.md), PR #31, `codegen_orchestrator-418` |
| 3. Small contract corrections | Fix narrow drift found by the audit before deeper hardening. | Audit findings have owner and evidence. | Worker-mode compose commands are proxied through `worker-start`/`worker-stop`; production scaffolding is pinned to GitHub `service-template` tag `0.3.0`; the local template mount is not part of production. | [contract audit §6.1-6.3](../reports/codegen-service-template-contract.md#61-makefile-patching-vs-a-native-orchestrator-api), [CHANGELOG 2026-07-12](../CHANGELOG.md#2026-07-12), PR #32, PR #33, `codegen_orchestrator-398`, `codegen_orchestrator-432` |
| 4. Sprint 002 phases 2-4 (ACTIVE) | Finish architectural hardening before autonomous recovery work. | CI and small service-template contract corrections are complete. | Response DTO enums, duplicated vocabularies, typed `RunResult`, typed Redis consume, dead-code removal and fail-fast conversions land phase by phase. PR #35 and #36 close the first two Phase 2 slices; typed `RunResult` is next. | [Sprint 002](../sprints/002-thermo-nuclear-hardening/sprint.md), [thermo review](../thermo-nuclear-review.md), [ROADMAP autonomy](../ROADMAP.md#autonomy-smart-steward) |
| 5. Deterministic mock smoke tests | Prove scaffold, worker-mode infra and generated app calls without live external services. | Sprint 002 phases 2-4 are complete or their touched contracts are stable enough for mock runs. | A repeatable mock smoke covers scaffold, setup, lint/tests, worker-start, smoke-probe, worker-call and cleanup with no resource leaks. | [contract audit §2](../reports/codegen-service-template-contract.md#2-deterministic-scaffold-smoke), `tests/integration/template/`, `tests/e2e/test_infrastructure_sanity.py` |
| 6. codegen + service-template matrix | Catch cross-repo drift before either side ships a breaking release. | Mock smoke is deterministic and the template tag bump process is documented. | Matrix runs current codegen against the pinned template tag and candidate template release, then records the resolved template commit. | [contract audit §4](../reports/codegen-service-template-contract.md#4-minimal-target-contract-v1), `scripts/system_configs.yaml`, `scheduler.service_template_ref` |
| 7. Live services | Validate provisioning, worker lifecycle, deploy and QA against real infrastructure after mock confidence. | Mock and matrix checks are green. | Live non-LLM services pass health, deploy and cleanup checks; failures produce typed outcomes rather than swallowed errors. | [DEV_PIPELINE](../DEV_PIPELINE.md), `make test-live`, `tests/live/` |
| 8. Telegram end-to-end | Put the user-facing Telegram path on top of the stabilized backend and live-service layers. | Live services are stable and Sprint 002 fail-fast work is complete. | Telegram request to deployed bot is verified last, with PO, scaffold, engineering, deploy and QA already covered underneath. | [CONTRACTS engineering flow](../CONTRACTS.md#engineering-flow), [ROADMAP stabilize core pipeline](../ROADMAP.md#stabilize-core-pipeline) |
