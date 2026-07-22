# Sprint Status

Current stabilization map: [docs/plans/codegen-stabilization-v1.md](plans/codegen-stabilization-v1.md).

Typed environment/secrets migration proposal: [typed env contract MVP](plans/typed-env-contract-mvp.md).

## Current Sprint
- **Sprint**: 002-thermo-nuclear-hardening
- **Goal**: Закрыть находки thermo-nuclear-review — типизированные границы, fail-fast, удаление мёртвого кода
- **Type**: tech
- **Started**: 2026-07-01
- **Current Phase**: Stage 7 live validation is complete — Mega 2.0 (PR #99) plus the
  2026-07-18..21 hardening wave (fail-closed teardown, commit-exact env contracts, title/slug
  split, typed deploy outcomes, waiting_for_user_secret loop, failure-safe dispatch). Remaining
  Stage 7 tail debt lives on the external board (600, 548, 676→527, 597, 673). Stage 8 (Telegram
  end-to-end) is next on the [stabilization map](plans/codegen-stabilization-v1.md); not started.

## Current Facts

- `codegen_orchestrator-646` splits project display text from runtime identity:
  projects store free-text `title` plus immutable server-generated unique `slug`.
  The API rejects client-supplied slug changes and migration `c7d8e9f0a1b2`
  backfills existing development rows from the old `name` column.

- `codegen_orchestrator-642` adds Codex as a developer-worker type end to end.
  `agent_type=codex` selects `worker-base-codex`, runs pinned Codex CLI 0.144.6
  through `codex exec --sandbox workspace-write`, and reports only through the
  existing localhost HTTP bridge. Host-session auth uses a dedicated validated
  read-write `HOST_CODEX_HOME`; unknown agent types fail instead of falling back
  to Claude. Image-chain and container smoke evidence is in
  [the task report](reports/codegen-orchestrator-642-codex-worker-runtime.md).

- Deploy resolves service-template 0.3.1 `POSTGRES_HOST_PORT` and `REDIS_HOST_PORT` from application
  allocations. Existing ports are reused and only missing infrastructure services are allocated.
- Runtime deploy connections now use the selected server's required `ssh_user` together with the
  SSH key from the same server handle. Initial provisioning and reinstall still bootstrap as root.

- Stage 7 live harness hardening records exact run-owned resources, verifies targeted cleanup and
  requires a separate non-LLM QA `passed` outcome after deploy.
- Stage 7 preflight now resumes crash-manifest cleanup fail-closed, resolves workers by ownership
  metadata, and tolerates idle Redis pubsub timeouts without ending LangGraph listeners.
- Mega 2.0 is complete in PR #99: the noop suite passed 7/7 and the live Claude path passed 5/5,
  including generated backend code, CI, merge, deploy success, `/health` 200 and non-LLM QA.
  PR #98 also releases `workspace:active_projects` in `delete_worker` cleanup even when Docker
  teardown fails.
- PR #102 (`codegen_orchestrator-618`) moved live cleanup to an internal API client: unowned
  deploy/QA runs are found, cancelled and confirmed terminal before external teardown. PR #103
  (`codegen_orchestrator-549`) makes parsed live-harness API reads fail on HTTP errors before body
  parsing.

- The 2026-07-18..21 hardening wave closed Stage 7: `head_sha` is required and env contracts read
  the deployed commit, not `main` (658, 661); deploy outcomes and env-contract dispatch are typed
  (620); server teardown lives in `shared/live_harness_remote_cleanup.sh` with parameters as
  arguments (666) and the standalone sweep works post-slug-migration (663); `wait_deploy` picks the
  web port by role (665); missing secrets force `WAITING_FOR_USER_SECRET` and the request loop is
  closed (670); task dispatch has no partial-state windows and recovers from finished runs (672,
  PR #125); `make lint` matches CI formatting (657).

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
| 5 | Deterministic mock smoke | COMPLETE — `codegen_orchestrator-496` |
| 6 | service-template compatibility matrix | COMPLETE — `codegen_orchestrator-499` |
| 7 | Live mega and cleanup hardening | COMPLETE — Mega 2.0 green (PR #99); 2026-07-18..21 wave closed teardown fences (645/659/662), head_sha env contracts (658/661), slug migration (646/647/663), teardown module (666), typed deploy outcomes (620), web port by role (665), waiting_for_user_secret (670), failure-safe dispatch (672). Tail debt on the board: 600, 548, 676→527, 597, 673 |

## Recent Stabilization Work

| Work | Status | Evidence |
|------|--------|----------|
| CI normalization | COMPLETE | PR #30, [CHANGELOG 2026-07-11](CHANGELOG.md#2026-07-11) |
| service-template contract audit | COMPLETE | PR #31, [contract audit](reports/codegen-service-template-contract.md) |
| worker-mode proxy target drift (`codegen_orchestrator-398`) | COMPLETE | PR #32, [CHANGELOG 2026-07-12](CHANGELOG.md#2026-07-12) |
| service-template production pin (`codegen_orchestrator-432`) | COMPLETE | PR #33 introduced the pin at `0.3.0`; current `scheduler.service_template_ref=0.3.5` |
| Stabilization sequence (`codegen_orchestrator-434`) | COMPLETE | [stabilization plan v1](plans/codegen-stabilization-v1.md) |
| B7 response-DTO enums (`codegen_orchestrator-435`) | COMPLETE | lifecycle fields on task/story/server/application/incident/service-deployment DTOs now use their `StrEnum`; slice of Phase 2 only |
| Unified contract vocabularies (`codegen_orchestrator-436`) | COMPLETE | `shared/contracts/vocab.py` canonical `AgentType`/`ActionType`/`ResultStatus`/`LifecycleEvent`; inline `Literal` sets removed, `error` synonym dropped; `WorkerCliKind`/`DeployAction`/`TaskType` kept distinct; tests in `shared/tests/unit/test_vocab.py` |
| Codex developer worker (`codegen_orchestrator-642`) | COMPLETE | `AgentType.CODEX`, strict project routing, dedicated image and host-session profile, non-interactive runner, full image-chain build and container smoke; [report](reports/codegen-orchestrator-642-codex-worker-runtime.md) |
| Typed `Run.result` union (`codegen_orchestrator-440`) | COMPLETE | `shared/contracts/dto/run_result.py` per-`RunType` models bound to `type`; producers emit typed models, scheduler reads typed attributes; invalid result → visible terminal state; tests in `shared/tests/unit/test_run_result.py` + `test_supervisor.py`. Closes Sprint 002 Phase 2 |
| Typed engineering consume + dead-layer removal (`codegen_orchestrator-457`) | COMPLETE | Engineering consumer on `EngineeringMessage.model_validate`; deleted `langgraph/src/tools/` (allocator → `allocations.py`), second `agent_config_cache`, `scaffold_phase.py`, `worker:lifecycle` stream+contract, shared compat-shims; tests in `test_engineering_validation.py`, `test_dead_layer_removed.py`, `test_phase3_shims_removed.py`. Closes Sprint 002 Phase 3. PR #42 |
| B3 incident journal reconciliation (`codegen_orchestrator-466`) | COMPLETE | Successful provisioning writes `READY` before journal closure. An unavailable journal remains observable and gets one warning; scheduler retries only active `provisioning_failed` entries for `READY` servers, idempotently and without recovery actions or per-tick notifications. |
| B4 secret resolver fail-fast (`codegen_orchestrator-473`) | COMPLETE | Resolver validates project context, allocations and repository metadata before deploy, rejects unknown computed values, and propagates secret-persistence failures through the deploy error path. The closeout audit tracks remaining Phase 4 boundaries separately. |
| Worker compose and provisioner outage bounds (`codegen_orchestrator-493`) | COMPLETE | Worker recipes preserve curl, JSON and compose failures and required override installation fails the task. Incident-journal reclaim retries only the journal write, then publishes one bounded terminal failure before ACK. |
| Sprint 002 closeout audit rerun (`codegen_orchestrator-495`) | COMPLETE, GREEN | [Original RED audit](reports/sprint-002-closeout-audit.md) is retained as historical evidence. The [rerun](reports/sprint-002-closeout-audit-rerun.md) verifies all five blocker slices on fetched `origin/main`. |
| Mega 2.0, live LLM worker (`codegen_orchestrator-627`) | COMPLETE | PR #99; noop 7/7 and live Claude 5/5, including deploy, `/health` and QA |
| Worker cleanup lock (`codegen_orchestrator-626`) | COMPLETE | PR #98; `workspace:active_projects` is released in `finally` and covered by a regression test |
| Live cleanup unowned runs (`codegen_orchestrator-618`) | COMPLETE | PR #102; internal ownership-aware run fence before teardown |
| Live harness fail-loud API reads (`codegen_orchestrator-549`) | COMPLETE | PR #103; parsed responses raise on HTTP errors before body parsing |

## Sprint History

| # | Goal | Type | Dates | Phases |
|---|------|------|-------|--------|
| 001 | Tech code hygiene (github split, noqa, secret storage) | tech | 2026-04-10 – parked | Phase 0/1 done; Phase 2 поглощена 002 |
