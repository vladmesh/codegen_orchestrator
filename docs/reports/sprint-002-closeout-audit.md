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
| Scaffolder process/auth boundary | `services/scaffolder/src/scaffold.py::_run_cmd`, `run_scaffold`, `run_ensure_workspace`. Queue-controlled `template_repo` and `repository_id` reach `create_subprocess_shell`; GitHub token is embedded in the URL and failed git stderr is logged and persisted. | Use argument-vector subprocesses; validate path ownership; pass git auth without a credentialed URL; redact persisted/logged git errors; regression tests cover metacharacters and a sentinel token. | None. This closes original B1 and the token-in-URL finding. |
| Worker compose proxy fail-fast | `packages/worker-wrapper/src/worker_wrapper/wrapper.py::_inject_makefile_overrides`. `curl | jq || echo` returns 0 when curl/jq fails; the override write itself is warning-only. | `worker-start`/`worker-stop` preserve a non-zero proxy/curl/jq exit and expose safe stderr; failure-semantics tests execute the generated recipe; a required override write cannot silently fall through to unusable Docker targets. | `codegen_orchestrator-433`; service-template worker-mode contract. |
| Provisioner outage terminal policy | `services/infra-service/src/main.py::run_worker`, `process_provisioner_job`, `provisioner/incidents.py::create_incident`, `provisioner/node.py::run`. Persistent incidents-API outage leaves the message unacked forever; after attempts are exhausted the same mandatory incident write still prevents a terminal result. | Bound reclaim attempts/age; do not redo external provisioning after the attempt budget; publish one visible terminal/escalated outcome and ACK or move to a DLQ-equivalent; test a multi-reclaim outage. | `codegen_orchestrator-472`; incident journal contract from PRs #44-#46. |
| Notification caller policy | Scheduler periodic workers in `health_checker.py`, `server_sync.py`, `github_sync.py`, `app_health_prober.py` call fail-fast `notify_admins` for outcome-independent alerts. A users-API outage aborts the rest of the tick and a deduped incident may never alert later. | Route outcome-independent alerts through `notify_admins_best_effort` or an explicit local boundary; tests prove one failed notification does not skip remaining entities. Keep outcome-gating notifications fail-fast only where documented. | `codegen_orchestrator-486`, PR #49. |
| Diagnostic secret safety | `shared/redis/client.py::connect` logs the complete `redis_url`; `services/langgraph/src/worker_events.py` logs `ValidationError.errors()` with input. | Never log URL userinfo or validation input; regression tests capture logs with sentinel credentials/payload values. Audit raw validation payload logs in `services/scaffolder/src/consumer.py` at the same time. | `codegen_orchestrator-447`; sanitizer pattern from PR #50. |

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
| B3 incident subsystem and reconciliation | partial | Typed infra API methods, atomic reservation, required failure-journal write, READY-before-resolve, scheduler reconciliation exist. | Infra/scheduler tests pass. Persistent journal outage still has no bounded terminal policy (`-472`). |
| B4 secret resolver | real | `SecretResolverNode` rejects missing/unknown project context, allocation, repository metadata and persistence failure before deploy. | 34 resolver tests in targeted LangGraph run pass. Downstream deployer still contains unrelated raw-dict defaults. |
| Required writes and swallow-list | partial | RAG embedding errors propagate; TASK/STORY writes propagate; PO repo/spec writes and `release_allocation` propagate. | Targeted tests pass. Required Makefile override write and git pull remain warning-only; compose recipe fabricates success. |
| Architect/consumer/infra API boundaries | real | Architect branches on HTTP status; `_base` distinguishes terminal validation; infra client methods raise; provisioning notifications are explicitly best-effort. | Targeted tests pass. The provisioner outage needs a bounded policy above the single-attempt boundary. |
| Notifications | partial | `notify_admins` now validates/raises and provisioning callers use `notify_admins_best_effort`. | Shared notification tests pass. Eleven scheduler calls still use the raising function for outcome-independent alerts (`-486`). |
| Runtime config fallbacks | real | Scheduler tasks use initialized `startup.get_config`; worker-manager validates worker URLs; dead langgraph `server_ip or host` response types were removed. | Startup/worker URL tests pass. Domain-level `public_ip or host` in provisioning is a separate intentional server-address rule, not a config default. |

## Required system classes

| Class | Verdict | Evidence and blast radius |
|---|---|---|
| Response enums/canonical vocabularies | real | Strict DTO lifecycle fields and shared vocab are live across scheduler/langgraph/infra callers. Remaining local literals are recorded below. |
| Typed `RunResult` | real | Producers construct typed models; supervisor validates the latest run and routes invalid terminal results to visible story failure/admin notification. |
| Redis consume terminal vs transient | partial | `consume_typed` terminally ACKs decode/validation poison; `_base` ACKs `TerminalMessageValidationError` and retains transient failures. Infra's required incident write has no terminal escalation after repeated transient failures. |
| Raw payload/secret diagnostics | partial | Typed Redis and PR #50 consumer validation sanitizers omit input. Full Redis URL, worker-event validation input and scaffolder invalid raw data remain loggable. Scaffolder also persists credentialed git stderr. |
| Dead/compat layers | partial | Phase 3 target list is mostly removed. Developer test shims, global tool context, wrapper archive shim, API aliases, infra re-exports and dead QA constant remain. |
| Swallowed exceptions/false success | partial | Named Phase 4 swallow list is mostly fixed. Worker compose recipes still return 0 on failure; required override/git-pull operations warn and continue. |
| Incidents/provisioning recovery | partial | Episode reservation and READY reconciliation are correct. Long incidents-API outage produces an unbounded PEL/reclaim loop and no terminal downstream result. |
| Secret resolution | real | Resolver is fail-fast and atomic with respect to computed-secret persistence. Scaffolder git credentials are a separate unresolved security boundary. |
| Mandatory writes | partial | RAG, PO and task/story writes are strict. Worker Makefile override and git refresh are still non-fatal even though later execution depends on them. |
| Notifications | partial | Core API has explicit raise vs best-effort functions, but scheduler callers have not been classified consistently. |
| Scheduler/worker-manager config | real | Runtime config is required at startup/call sites and covered by tests. |

## Original thermo-review findings

### P0, P1 typed boundaries and security

| Finding | Verdict | Files/symbols and check | Residual risk |
|---|---|---|---|
| B1 scaffolder shell injection | not done | `scaffold.py::_run_cmd` still calls `create_subprocess_shell`; `run_scaffold` interpolates template/path/ref values. | Queue input can alter shell commands. Stage 4 blocker. |
| B2 crypto plaintext fallback/secret prefix | real | `shared/crypto.py::SecretsCipher.decrypt`; unit suite. | No plaintext return or prefix log remains. |
| B3 dead incidents subsystem | partial | Typed incident methods/upsert exist and tests pass. | Persistent API outage still wedges the job (`-472`). |
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
| `codegen_orchestrator-486` | Stage 4 blocker | PR #49 migrated only provisioning/recovery callers, so the sprint's notifications claim is partial. |
| PR #49 | partial | Explicit architect/base/infra boundaries are real. Caller-policy split remains. `gh pr view` shows no retained GitHub comments/reviews. |
| PR #50 | real | PO logs use safe validation sanitizer and delayed context binding; sentinel tests pass. Adjacent validation logs were out of slice. |
| PR #51 | real for its slice | Scheduler config and worker URLs fail at startup; API types restored. It does not close domain or security blockers above. |

## Stage 5 entry conditions after fixes

Rerun this audit on the fix SHA, with regression tests for every blocker scenario. Only then may the
plan switch Stage 4 to complete and start Stage 5. The deterministic smoke must execute generated
worker-mode recipes and assert non-zero failure propagation; merely importing a module before a
semicolon-delimited sleep is not evidence.
