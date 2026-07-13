# Sprint 002 closeout audit rerun

- Date: 2026-07-14
- Audited commit: `b0463fb34473c2756a40b669d2b7c5559b02d486` (`origin/main` after
  `git fetch origin main`)
- Scope: the five Stage 4 blockers in the 2026-07-13
  [RED audit](sprint-002-closeout-audit.md), plus the Phase 2-4 system-class matrix.
- Method: fetched-code inspection and live local regression tests. Card reports and prior audit
  claims were leads only. The checkout matched the fetched ref, but the SHA above, not the local
  checkout, is the source of truth.

## Verdict

**GREEN — Stage 4 exit gate выполнен.**

All five prior blocker slices are closed on the audited SHA and their regression evidence is green.
The 2026-07-13 RED audit remains historical evidence, including its original failure probes; it is
not rewritten. Stage 5 is next. E2E remains pending until it has its own evidence.

## Reproduced verification

| Command | Result | Purpose |
|---|---|---|
| `git fetch origin main && git rev-parse origin/main` | pass | `b0463fb34473c2756a40b669d2b7c5559b02d486` |
| `make lint` | pass | Ruff reported `All checks passed!` |
| `make ci-contract` | pass | `CI gate contract ok` |
| `make test-unit` | pass | Canonical isolated eight-suite local unit run |
| Canonical CI for PRs #53, #54 and #55 | pass | Each merged PR has successful Fast Checks, CI Contract, all affected service jobs, integration jobs and Required CI Gate; PR #55 is the audited `origin/main` tip. |
| `PYTHONPATH=services/scaffolder uv run pytest services/scaffolder/tests/unit/test_scaffold.py -q` | pass, 16 | Scaffolder argv, validation, path ownership and secret redaction |
| `PYTHONPATH=services/infra-service uv run pytest services/infra-service/tests/unit/test_incident_outage_policy.py services/infra-service/tests/unit/test_incidents.py services/infra-service/tests/unit/test_provisioning_attempts.py -q` | pass, 17 | Journal outage budget, reclaim, duplicate result, publish and ACK paths |
| `PYTHONPATH=services/worker-manager uv run pytest services/worker-manager/tests/unit/test_compose_runner.py -q` | pass, 14 | Compose command construction and proxy result propagation |
| `PYTHONPATH=. uv run pytest packages/worker-wrapper/tests/unit/test_makefile_overrides.py shared/tests/test_redis_client.py shared/tests/test_notifications.py -q` | pass, 65 | Required recipe overrides, Redis poison diagnostics and notification boundary |
| `PYTHONPATH=services/scheduler uv run pytest services/scheduler/tests/unit/test_server_sync.py services/scheduler/tests/unit/test_health_checker.py services/scheduler/tests/unit/test_app_health_prober.py services/scheduler/tests/unit/test_github_sync.py services/scheduler/tests/unit/test_supervisor_run_routing.py -q` | pass, 49 | Caller-policy sweep continuation and scheduler adjacent paths |
| `PYTHONPATH=services/langgraph uv run pytest services/langgraph/tests/unit/consumers/test_engineering_blocked.py services/langgraph/tests/unit/consumers/test_engineering_reject.py services/langgraph/tests/unit/test_blocked_flow_e2e.py -q` | pass, 11 | LangGraph notification continuation |

The targeted commands used the same clean environment and per-service `PYTHONPATH` isolation as
`scripts/test-unit-local.sh`. A combined multi-service collection was not used as import evidence:
services share a top-level `src` package. The scheduler suite emitted two pre-existing unawaited
`AsyncMock` warnings in uptime-calculation tests; all assertions passed and the warnings are not a
Stage 4 failure scenario.

## Previous blocker slices

| Slice | Verdict | Symbols and reproduced failure scenario | Regression evidence |
|---|---|---|---|
| Scaffolder argv, path, auth and redaction | closed | `services/scaffolder/src/scaffold.py::{_run_cmd,_workspace_path,run_scaffold,run_ensure_workspace}` uses `asyncio.create_subprocess_exec` with argv, rejects traversal and symlink escape before execution, and uses credential-safe Git auth plus diagnostic redaction. Queue values with spaces and `;$(id)` remain a single argv entry; malicious modules/project names never reach a subprocess; sentinel credentials in URL, Bearer payload and stderr do not reach a result or log. | `test_scaffold.py`: `test_queue_values_are_preserved_as_single_argv_entries`, `test_workspace_traversal_and_symlink_escape_stop_before_exec`, `test_subprocess_failure_redacts_credentials_from_result_and_logs`, malicious-input tests, 16 passed. |
| Redis and validation diagnostics | closed | `shared/redis/client.py::consume_typed` and `_terminal_ack`, `shared/diagnostics.py::safe_validation_errors`, and the worker/scaffolder validation callers retain terminal-vs-transient handling without recording raw payloads or Redis credentials. Malformed JSON/schema payloads are terminally ACKed; ACK failure leaves the consumer alive. | `shared/tests/test_redis_client.py`: broken JSON, invalid schema, raw-payload exclusion and terminal-ACK tests, included in 65 passed. |
| Generated worker recipes and required override | closed | `packages/worker-wrapper/src/worker_wrapper/wrapper.py::_install_makefile_overrides` raises on required-write failure. Generated recipes preserve curl transport, HTTP/JSON result errors and non-zero compose exit instead of the old pipeline false success. | `test_makefile_overrides.py`, 65-test shared/wrapper run; `services/worker-manager/tests/unit/test_compose_runner.py`, 14 passed. |
| Persistent incident-journal outage | closed | `services/infra-service/src/main.py::{_handle_incident_outage,_retry_saved_incident}` records a journal-only retry budget. It does not rerun provisioning on reclaim; after the budget it emits one terminal failure then ACKs. Failed terminal publish stays unacked for one retry, and failed ACK retries without a duplicate terminal publish. | `test_incident_outage_policy.py::{test_outage_budget_emits_one_terminal_result_then_acks,test_terminal_publish_failure_leaves_entry_unacked_for_a_single_retry,test_terminal_ack_failure_retries_ack_without_second_terminal_publish,test_reclaimed_journal_retry_does_not_repeat_provisioning}`, 17 passed. |
| Notification caller inventory and sweep continuation | closed | `shared/notifications.py::notify_admins_best_effort` is the safe boundary for outcome-independent alerts. Scheduler `health_checker`, `server_sync`, `github_sync`, `app_health_prober`, `supervisor`; LangGraph engineering result handling; and infra provisioning/recovery invoke it only after their authoritative state mutation. A users API failure logs safe metadata and cannot abort a multi-entity tick. `notify_admins` remains raising for a future outcome-gating caller. | `shared/tests/test_notifications.py::TestBestEffortNotifications`, `test_server_sync.py::test_force_rebuild_sweep_continues_after_first_notification_failure`, adjacent scheduler and LangGraph caller tests: 65, 49 and 11 passed. |

## Phase 2-4 system-class matrix

| System class | Verdict | Evidence and residual classification |
|---|---|---|
| Typed DTO, vocabularies and `RunResult` | closed | Strict DTO enums, `shared/contracts/vocab.py` and `RunDTO` discriminated result validation remain live. Existing unit gate passes. Nested deploy/smoke details and API-local schema duplication are Stage 5+ contract debt, not a Stage 4 failure scenario. |
| Redis terminal vs transient policy | closed for Stage 4 | `consume_typed` terminally ACKs decode/schema poison, `_base` preserves transient failure semantics, and outage escalation now terminates bounded journal-only reclaim. Raw APIs remain a deferred migration surface, not an observed bypass in the audited blockers. |
| Mandatory writes and false success | closed for Stage 4 | Required Makefile override installation and compose execution now fail visibly. Git refresh remains warning-only by deliberate policy and is not a required write in this gate. |
| Incident journal and reconciliation | closed | Provisioning writes `READY` before closure; scheduler reconciles active `provisioning_failed` incidents for `READY` servers. The new outage policy covers multi-reclaim, duplicate terminal publish and ACK failure. |
| Secret resolution and diagnostics | closed for Stage 4 | Secret resolver remains fail-fast. Scaffolder and Redis/validation diagnostics exclude credential/payload sentinels. No regression from the new shared sanitizer was found. |
| Notifications | closed | The complete live caller inventory is classified: best-effort after independent state mutation, raising primitive retained without a current owner. Multi-entity continuation is covered. |
| Runtime configuration | closed | Scheduler and worker URLs still validate required values at startup/call boundaries; no default-value regression was introduced by PRs #53-#55. |

## Non-blocking follow-up

The original audit's P2 structural debt remains deferred: API task transition duplication, local/shared
wire-schema overlap, scheduler retry-policy consolidation, broad worker/supervisor ownership and
large-module decomposition. These are not promoted to Stage 4 blockers because this rerun did not
reproduce a concrete Stage 4 failure scenario from them. Stage 5 starts with deterministic mock
smoke; live services and Telegram E2E remain out of scope and pending.
