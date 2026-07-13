# Sprint 002 closeout audit

- Date: 2026-07-13
- Audited commit: `4c501ea378aa7eacc2511e95e95645f322376fdd` (`origin/main` after
  `git fetch origin main`)
- Scope: Sprint 002 Phases 2-4, every finding in
  [thermo-nuclear-review.md](../thermo-nuclear-review.md), adjacent live callers, PRs #49-#51 and
  board ideas `codegen_orchestrator-392`, `-433`, `-447`, `-460`, `-472`, `-486`
- Method: current-code inspection, history/PR inspection, board task inspection, canonical and
  targeted tests. The old review and sprint checkboxes were treated as leads, not evidence.

## Verdict

**RED — Stage 4 remains active.**

Phases 2 and 3 are substantially implemented and tested. Most named Phase 4 slices are also real,
but the Stage 4 exit gate is not met. Current `main` still has security-critical shell execution and
credential leakage in scaffolding, false-success worker-mode compose recipes, an unbounded
provisioner reclaim path when the incidents API stays down, incomplete notification error-policy
migration, and unsafe diagnostic logging of a credential-bearing Redis URL. These are live paths,
not documentation drift.

The green local suite proves that the implemented contract slices are internally consistent. It
does not cover the failure scenarios above. Stage 5 deterministic mock smoke must not be treated as
the next active stage until the blocker slices below land and this audit is rerun.

### Minimal blocker slices

| Slice | Files and failure scenario | Acceptance criteria | Dependencies |
|---|---|---|---|
| Scaffolder process/auth boundary | Closed by `codegen_orchestrator-492`: `scaffold.py` runs argv vectors, rejects workspace traversal/symlink escapes, uses Git's HTTP extra header and redacts diagnostics before result/log persistence. | Targeted regression tests cover metacharacters, escaped workspaces and credential sentinels. | No remaining dependency for this slice. |
| Worker compose proxy fail-fast | Closed by `codegen_orchestrator-493`: generated recipes preserve curl, JSON and compose-proxy failures and write safe proxy stderr; required override installation fails the worker task. | Executed fake-proxy recipe tests cover success, transport, invalid JSON, missing result and non-zero compose exit. | No remaining dependency for this slice. |
| Provisioner outage terminal policy | Closed by `codegen_orchestrator-493`: a reclaimed journal outage retries only the required incident write, not Ansible or server creation. After the bounded budget it publishes one terminal failure and ACKs. | Unit tests cover bounded retry, recovered journal write, duplicate delivery after terminal publish and ACK behavior. | No remaining dependency for this slice. |
| Notification caller policy | Closed by `codegen_orchestrator-494`: periodic scheduler alerts, supervisor terminal alerts, langgraph worker-gave-up alerts, and provisioning/recovery alerts all use the shared best-effort boundary after their state mutation. `notify_admins` remains the raising primitive for a caller that explicitly needs delivery to gate its outcome; no live caller currently has that ownership. A users-API outage no longer aborts a sweep. | Candidate for the next closeout audit; this card does not declare Stage 4 green. | `codegen_orchestrator-494`. |
| Diagnostic secret safety | Closed by `codegen_orchestrator-492`: Redis connect diagnostics omit the URL; worker/scaffolder validation errors use the shared type/loc sanitizer; scaffold failures redact URLs, authorization headers and known tokens. | Targeted regression tests capture credential sentinels from logs and persisted scaffold results. | No remaining dependency for this slice. |

## Verification record

| Check | Result | Notes |
|---|---|---|
| `git fetch origin main && git rev-parse origin/main` | pass | Produced audited SHA above; checkout was exactly even with fetched `origin/main`. |
| `make lint` | pass | `All checks passed!` |
| `make test-unit` | pass | 8 suites passed, 0 failed. The script isolates each service's `src` package. |
| `make ci-contract` | pass | `CI gate contract ok`. |
| Contract/Redis/worker targeted suite | pass | 113 tests: vocab, typed run result, Redis typed consume, worker result, Phase 3 shims, notifications. |
| LangGraph targeted suite | pass | 105 tests: engineering validation, dead layers, secret resolver, terminal consumer validation, PO sanitizer, architect boundary. |
| Infra/scheduler/config targeted suites | pass | 10 infra, 8 scheduler, 3 worker-manager tests. |
| API RAG and wrapper targeted suites | pass | 1 RAG test and 24 wrapper tests. |
| Generated compose recipe failure probe | reproduced | `false | false || echo ...` exits 0, matching the generated pipeline. Existing wrapper tests inspect text but never execute failure semantics. |
| Compose import gotcha | accounted for | No verdict relies on `python -c 'import src.main'; sleep ...`; the semicolon allows sleep after a failed import. |
| Live services/E2E | not run | Out of scope. E2E remains pending and is not inferred from unit/service evidence. |

One attempted combined `pytest` command failed collection because multiple services expose a
top-level `src` package. It was rerun with the same per-service `PYTHONPATH` isolation used by
`scripts/test-unit-local.sh`; all listed targeted suites then passed.

## Phase 2-4 claims

In these tables, `real` means the claimed implementation exists on the audited SHA and has direct
test evidence; `partial` means only part of the claim is implemented; `stale` means the old finding
no longer applies because the owning path disappeared or changed; `not done` means the finding is
still reproducible.

| Phase claim | Verdict | Current evidence | Verification and residual risk |
|---|---|---|---|
| B7 response DTO enums | real | `shared/contracts/dto/{task,story,server,application,incident,service_deployment}.py`: lifecycle fields use their `StrEnum` types. | `shared/tests/unit/test_vocab.py`; unknown values reject. API-local duplicate schemas remain a P2 cleanup, not this slice. |
| Canonical vocabularies | real | `shared/contracts/vocab.py`: `AgentType`, `ActionType`, `ResultStatus`, `LifecycleEvent`; field-specific subsets remain narrow. | Vocab targeted tests pass. Several distinct axes and older schemas still use local literals by design or as deferred debt. |
| Typed `RunResult` per `RunType` | real | `shared/contracts/dto/run_result.py`; `RunDTO._validate_result_for_type`; supervisor reads typed attributes. | `test_run_result.py` and scheduler unit suite pass. Nested `deployment_result`/`smoke_result` remain raw dicts. |
| `consume_typed` and poison handling | real | `shared/redis/client.py::consume_typed`, `_terminal_ack`; validation diagnostics omit input. | Redis targeted tests pass. Raw `publish`, `publish_flat` and `consume` remain live escape hatches. |
| Typed worker result end-to-end | real | `shared/contracts/queues/worker_result.py`; wrapper `publish_message`; `worker_spawner.spawn_result_from_output`. | Worker contract/mapping tests pass. The create-worker ACK is still raw `.get()` handling. |
| Typed engineering consume | real | `services/langgraph/src/consumers/engineering.py::process_engineering_job` validates `EngineeringMessage` before business logic. | Engineering validation tests pass, including terminal invalid input. |
| Phase 3 dead-layer removal | partial | `langgraph/src/tools/`, second agent config cache, worker lifecycle contract, worker-manager scaffold phase and shared Redis compat import are gone. | Dead-layer tests pass. Other original dead/compat layers remain, listed below. |
| B3 incident subsystem and reconciliation | real for the outage boundary | Typed infra API methods, atomic reservation, required failure-journal write, READY-before-resolve, scheduler reconciliation and bounded journal-outage escalation exist. | Infra tests cover retry without reprovisioning and one terminal result. |
| B4 secret resolver | real | `SecretResolverNode` rejects missing/unknown project context, allocation, repository metadata and persistence failure before deploy. | 34 resolver tests in targeted LangGraph run pass. Downstream deployer still contains unrelated raw-dict defaults. |
| Required writes and swallow-list | partial | RAG embedding errors propagate; TASK/STORY writes propagate; PO repo/spec writes and `release_allocation` propagate. | Required Makefile override installation and compose recipe failures are fail-fast. Git refresh remains warning-only. |
| Architect/consumer/infra API boundaries | real | Architect branches on HTTP status; `_base` distinguishes terminal validation; infra client methods raise; provisioning notifications are explicitly best-effort. | Targeted tests pass. The provisioner outage needs a bounded policy above the single-attempt boundary. |
| Notifications | real | `notify_admins` is the documented raising primitive; outcome-independent callers use `notify_admins_best_effort`, which logs one safe failure record and does not promise delivery. | Caller inventory and sweep-continuation tests in `codegen_orchestrator-494`; rerun the closeout audit before declaring Stage 4 green. |
| Runtime config fallbacks | real | Scheduler tasks use initialized `startup.get_config`; worker-manager validates worker URLs; dead langgraph `server_ip or host` response types were removed. | Startup/worker URL tests pass. Domain-level `public_ip or host` in provisioning is a separate intentional server-address rule, not a config default. |

## Required system classes

| Class | Verdict | Evidence and blast radius |
|---|---|---|
| Response enums/canonical vocabularies | real | Strict DTO lifecycle fields and shared vocab are live across scheduler/langgraph/infra callers. Remaining local literals are recorded below. |
| Typed `RunResult` | real | Producers construct typed models; supervisor validates the latest run and routes invalid terminal results to visible story failure/admin notification. |
| Redis consume terminal vs transient | partial | `consume_typed` terminally ACKs decode/validation poison; `_base` ACKs `TerminalMessageValidationError` and retains transient failures. Infra's required incident write has no terminal escalation after repeated transient failures. |
| Raw payload/secret diagnostics | real for the Stage 4 boundary | `codegen_orchestrator-492` removes Redis URL and worker/scaffolder validation-input exposure; scaffold diagnostics pass through shared redaction before logging or persistence. Other unrelated diagnostic surfaces remain outside this slice. |
| Dead/compat layers | partial | Phase 3 target list is mostly removed. Developer test shims, global tool context, wrapper archive shim, API aliases, infra re-exports and dead QA constant remain. |
| Swallowed exceptions/false success | partial | Named Phase 4 swallow list is mostly fixed. Worker compose recipes and required override installation are fail-fast; git refresh remains warning-only. |
| Incidents/provisioning recovery | real for the outage boundary | Episode reservation and READY reconciliation are correct. Long incidents-API outage has a bounded journal-only reclaim path and one terminal downstream result. |
| Secret resolution | real | Resolver is fail-fast and atomic with respect to computed-secret persistence. Scaffolder git credentials are a separate unresolved security boundary. |
| Mandatory writes | partial | RAG, PO, task/story and worker Makefile override writes are strict. Git refresh remains non-fatal. |
| Notifications | partial | Core API has explicit raise vs best-effort functions, but scheduler callers have not been classified consistently. |
| Scheduler/worker-manager config | real | Runtime config is required at startup/call sites and covered by tests. |

## Original thermo-review findings

### P0, P1 typed boundaries and security

| Finding | Verdict | Files/symbols and check | Residual risk |
|---|---|---|---|
| B1 scaffolder shell injection | real (`codegen_orchestrator-492`) | `scaffold.py::_run_cmd` uses `create_subprocess_exec` with argv vectors; both workspace paths are owned and checked before side effects. | This blocker slice is closed; Stage 4 remains RED for the other blocker groups until a fresh audit. |
| B2 crypto plaintext fallback/secret prefix | real | `shared/crypto.py::SecretsCipher.decrypt`; unit suite. | No plaintext return or prefix log remains. |
| B3 dead incidents subsystem | real for the outage boundary | Typed incident methods/upsert and bounded journal-only reclaim exist. | Persistent journal outage emits one terminal result instead of wedging the PEL. |
| B4 secret resolver fake values | real | `SecretResolverNode` and 34 tests. | Missing/unknown inputs fail before deploy. |
| B5 dead `langgraph/src/tools` | real | Directory absent; dead-layer tests. | Other dead layers are separate rows. |
| B6 raw worker result | real | Discriminated `WorkerResult`, typed publish, shared mapper. | Create ACK remains raw. |
| B7 stringly response DTOs | real | DTO enum fields and vocab tests. | Local API schema duplication is P2 debt. |
| API fail-open auth | real | `require_internal_or_admin`, service clients and suites. | `-392` is a separate name-shadowing 500. |
| Scaffolder token in URL/stderr | not done | Both scaffold paths build credentialed URLs; stderr enters result/logs. | Direct credential disclosure. Stage 4 blocker. |
| Engineering consumer raw unpack | real | `engineering.process_engineering_job`; validation tests. | None in this row. |
| `Run.result: dict` | real | `RunDTO.result`, `_RESULT_MODEL_BY_TYPE`; tests. | Nested deploy/smoke details are dicts. |
| Redis raw consume/JSON swallow | partial | `consume_typed` is strict and tested; raw methods remain live. | Callers can bypass typed boundary. |
| Scheduler result guessing | real | Supervisor uses typed outcome attributes. | File remains overloaded. |
| Smoke/deployer result dicts | not done | `smoke.py`, `DeployRunResult.smoke_result/deployment_result`. | Stage 5+ contract work. |
| Callback event strings | not done | `_events.py` accepts strings; PO repeats `_STORY_EVENTS`. | Producer/consumer drift. |
| Engineering result lifecycle strings | partial | Run writes are typed; event/result helpers still accept strings/dicts. | Boundary not fully eliminated. |
| Env-var class strings | not done | `env_analyzer.py`, `secret_resolver.py`; no `EnvVarClass`. | Classification drift. |
| Server update raw dict | stale | The cited monolithic handler no longer exists in that shape; public updates use typed schemas. | Generic internal payloads remain elsewhere. |
| Telegram PO raw response | not done | `telegram_bot/main.py` checks `data.get("error") == "true"`. | Malformed replies can be misclassified. |
| Project spec/application ports dicts | not done | `ProjectDTO.project_spec`, `ApplicationDTO.ports`. | Response schema drift. |
| API composite responses | partial | Some response models landed; composite task/application/server payloads remain ad hoc. | Deferred contract cleanup. |
| Worker create ACK `.get()` | not done | `worker_spawner.request_spawn` reads raw response. | Invalid ACK looks like ordinary failure. |
| Duplicated vocabularies | real | `shared/contracts/vocab.py`, strict subsets and tests. | Runtime agent taxonomy remains separate. |

### P1 fail-fast findings

| Finding | Verdict | Files/symbols and check | Residual risk |
|---|---|---|---|
| RAG embeddings swallowed | real | Embedding errors propagate; incomplete result raises; API test. | None. |
| TASK/STORY writes swallowed | real | Direct writes; wrapper tests. | Other workspace prerequisites still warn. |
| PO repository/spec writes swallowed | real | Current direct API calls and PO tests. | Scaffolder repo-collision swallow is separate. |
| Architect text-matches `422` | real | HTTP status branch and targeted tests. | None. |
| Base consumer poison loop | real | Terminal validation type, safe log and ACK tests. | Transient errors intentionally remain pending. |
| Infra API fallback returns | real | Typed requests raise; infra tests. | Bounded retry remains missing. |
| `release_allocation` returns false | real | Delete errors propagate. | None. |
| Notifications return zero on outage | real | `notify_admins` validates and raises. | Caller policy is partial/blocking. |
| Magic-number config defaults | real | Scheduler startup and worker URL tests. | None in named slice. |

### P2 dead code

| Finding | Verdict | Files/symbols and check | Residual risk |
|---|---|---|---|
| Legacy tools/results | real | Removed; tests. | None. |
| `worker:lifecycle` | real | Contract/channel/callers absent. | None. |
| Second config cache | real | File absent. | None. |
| Developer identity wrappers | not done | `DeveloperNode` retains seven test-only delegates. | Indirection. |
| Global `state/context.py` | not done | File/global state remain. | Process-global coupling. |
| `_collect_and_archive` | not done | Wrapper method remains. | Dead concept. |
| API task aliases/`__all__` | not done | Router export block remains. | Compat surface. |
| Dead QA constant/branch | partial | `MAX_QA_LOOPS` remains unused; boolean branch has narrowed. | Misleading dead code. |
| Infra re-exports/singleton/attempt helper | partial | Re-exports/singleton remain; attempt reservation is now live. | Original retry diagnosis is stale, compat clutter remains. |
| Shared compat shims | real | Named aliases/import fallback removed; tests. | Canonical deployment model remains by design. |

### P2 abstractions, file size and atomicity

| Finding | Verdict | Files/symbols and check | Residual risk |
|---|---|---|---|
| API task transition ritual | not done | `_task_actions.py` repeats transition/event/commit. | Divergent event semantics. |
| Router helper duplication | partial | Task helper exists, no generic shared entity helper. | HTTP/behavior drift. |
| Duplicate task wire schemas | not done | API-local and shared DTOs coexist. | Two sources of truth. |
| Scheduler retry/timeout policy | not done | Several counters/status loops remain. | Inconsistent watchdogs. |
| Two LangGraph API clients/methods | not done | PO raw paths and duplicate deployment methods remain. | Contract duplication. |
| Worker agent branching | not done | Wrapper and manager runners share ownership. | Branch growth. |
| Deploy epilogue duplication | not done | Main/rerun finalization remains repeated. | Partial state drift. |
| Dashboard URL duplication | not done | No canonical helper. | Security-sensitive drift. |
| Classification/repo-env duplication | partial | Classifier helper is reused; environment policy remains split. | Half fixed. |
| Wrapper decomposition | not done | 871 lines, mixed concerns. | Structurally overloaded. |
| Supervisor decomposition | not done | 692 lines, three domains. | Grew since review. |
| Telegram main decomposition | not done | 531 lines. | Coupled plumbing/handlers. |
| `OrchestratorState` split | partial | Graph shrank, broad multi-flow state remains. | Diffuse ownership. |
| Atomic task retry | not done | BACKLOG, TODO, iteration are three API calls. | Crash can cause duplicate dispatch. |
| `delete_worker` lock leak | not done | Lock release follows container/network deletion inside outer try. | Cleanup failure blocks project. |
| Worker failure status source | partial | FAILED is written, separate cleanup can delete status/meta. | Poller may see UNKNOWN. |
| Synthetic complete events | not done | `_COMPLETE_PATH` writes intermediate events without intermediate work. | Fictional journal history. |

### Lower-priority findings

| Finding | Verdict | Current classification |
|---|---|---|
| Function-local API imports | not done | Stage 5+ maintainability debt. |
| Scheduler private Redis access | partial | Isolated private access remains. |
| Scheduler `user_id=""` | not done | Still built in `dispatch_todo_tasks`. |
| Story transition actor attribution | not done | Reliable actor model is absent. |
| Internal project UUID | not done | Hardcoded in dispatcher. |
| Ignored keyboard argument | not done | Unrelated UI cleanup. |
| N+1 fetches | not done | Sequential sibling/event and cited API fetches remain. |
| Duplicate project update verbs | not done | PUT/PATCH duplication remains. |
| Local `HTTP_OK` | partial | Most touched code uses `HTTPStatus`; isolated constants remain. |
| Worker stream-name duplication | partial | Lifecycle removed; queue/channel concepts still overlap. |
| Worker unions lack discriminator | partial | `WorkerResult` is discriminated; older command/response unions remain. |

## Board ideas and PR #49-#51 follow-up

| Item | Classification | Reason |
|---|---|---|
| `codegen_orchestrator-392` | separate reliability debt | Confirmed name-shadowing 500 in two unauthenticated list endpoints. Outside Phase 2-4 and not a duplicate of fail-open auth. |
| `codegen_orchestrator-433` | Stage 4 blocker | Reproduced false success in generated worker-mode recipes. |
| `codegen_orchestrator-447` | Stage 4 security blocker | Full Redis URL with password/token is logged. |
| `codegen_orchestrator-460` | Stage 5+ reliability follow-up | Dispatcher create-run/publish/transition is non-atomic and can create orphan runs. Serious P2 orchestration debt; address before live-service stages. |
| `codegen_orchestrator-472` | Stage 4 blocker | A correct transient single-attempt policy becomes an infinite reclaim loop during a persistent outage. |
| `codegen_orchestrator-486` | Closed by successor | `codegen_orchestrator-494` completes the scheduler and workflow caller-policy migration. |

### Notification caller inventory (`codegen_orchestrator-494`)

| Caller group | Policy | Reason |
|---|---|---|
| Scheduler `health_checker`, `server_sync`, `github_sync`, `app_health_prober` | outcome-independent, shared best-effort | The incident, status, repository, or provisioning trigger has already been persisted before alert delivery. |
| Scheduler `supervisor`, `provisioner_result_listener` | outcome-independent, shared best-effort | The terminal story/server result is committed before the diagnostic alert. |
| Langgraph `engineering_result_handler` | outcome-independent, shared best-effort | The task and story transition to human review precedes the admin alert. |
| Infra provisioner handlers, operations, node, recovery | outcome-independent, shared best-effort | Provisioning/recovery state and journal ownership are independent of Telegram/users API delivery. |
| `notify_admins` primitive | outcome-gating primitive, currently no live owner | It propagates users API/config/validation failures for a future caller whose outcome explicitly owns notification delivery. |
| PR #49 | partial | Explicit architect/base/infra boundaries are real. Caller-policy split remains. `gh pr view` shows no retained GitHub comments/reviews. |
| PR #50 | real | PO logs use safe validation sanitizer and delayed context binding; sentinel tests pass. Adjacent validation logs were out of slice. |
| PR #51 | real for its slice | Scheduler config and worker URLs fail at startup; API types restored. It does not close domain or security blockers above. |

## Stage 5 entry conditions after fixes

Rerun this audit on the fix SHA, with regression tests for every blocker scenario. Only then may the
plan switch Stage 4 to complete and start Stage 5. The deterministic smoke must execute generated
worker-mode recipes and assert non-zero failure propagation; merely importing a module before a
semicolon-delimited sleep is not evidence.
