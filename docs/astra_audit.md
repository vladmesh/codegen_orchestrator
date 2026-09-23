# Astra architecture audit

Date: 2026-09-23  
Audited branch: main at fe9ab1e6 (after PR #582, sprint:1457), deployed to production 2026-09-23T10:20Z  
Previous refresh: 2026-09-23 morning, through 9e8a4f00 (after PR #577)  
Scope: architecture, service/process boundaries, legacy and compatibility code, fallbacks, hidden coupling, operational complexity, and removable technical debt.

## Executive summary

The repository changed substantially after the 2026-09-12 refresh: more than two hundred commits landed on main before this re-audit. The codebase is stronger in several important failure boundaries than it was when this audit was first written:

- engineering dispatch admission is increasingly transactional and evidence-driven;
- pre-agent refusals, infrastructure parks and owner notifications have durable recovery paths;
- developer workers use repository-scoped GitHub tokens;
- story-owned and gave-up workers have explicit teardown/reconciliation;
- temporary QA access has target-scoped admission and an audited operator drain;
- long-lived story states gained bounded watchdogs and stage notices;
- generated-product and live-test evidence now checks substantially more residue and artifact boundaries;
- CI imports production service entrypoints from built images, catching missing runtime dependencies before merge.
- Time4VPS scheduler reconciliation now reuses one bounded HTTP connection pool per sync cycle instead of creating a fresh pool for every provider request.
- The GitHub App client and the embedding client own one bounded HTTP pool per operation (sprint:1453, PRs #568/#569), the legacy `mega-test` sweep prefix is retired after a recorded read-only production inventory (PRs #566/#570), and ARCHITECTURE.md/AGENTS.md match the code again (PR #567).

Those improvements do not invalidate the main architectural concern from the original audit. The scheduler process split from PR #485 remains useful, and PR #563 removed terminal/gave-up worker teardown reconciliation from the order-sensitive dispatcher tick into its own scheduler-pipeline worker. The remaining dispatcher still owns one large ordered routing/supervision cycle whose sequencing is part of the product contract.

### Current status

Of the original sixteen H/M/L findings:

- **14 are complete:** H2, H3, H4, M1, M2, M3, M4, M5, M6, M7, M8, L1, L2 and L4.
- **H1 remains partially complete, three responsibilities out:** PR #563 extracted worker teardown reconciliation; PRs #572/#573 (sprint:1456) moved temporary-access cleanup out on a durable QA-routing guard; PRs #578/#579 (sprint:1457) moved owed owner-notification delivery out on a durable per-record last-attempt fact with an atomic spacing claim. Nine ordered steps remain in the dispatcher tick.
- **M5 is complete via PR #571 (sprint:1454):** the legacy temporary-access schema, model fields, router branches, tests and contract text are gone; production runs migration 4d8e1f2a3b5c.
- **L3 is effectively closed:** Time4VPS scheduler sync, the GitHub App client, the embedding client, and now the scaffolder as the first multi-call adopter of the GitHub lifecycle (PR #574, singleton removed). Remaining per-call clients are deliberate one-shot probes; further adoption happens when a caller is touched, not as audit work.

This refresh adds three findings:

- **M9 — complete via PRs #559, #560 and #562:** Codex 0.144.6 and Claude Code 2.1.278 have named versioned profile adapters and worker-image version gates; PR #562 moved the remaining Codex serde_json/JWT behavior behind the Codex adapter and added explicit compatibility provenance.
- **M10 — complete:** PR #554 removed the server-sync exemption, PR #555 decomposed the LangGraph deploy consumer, and PR #557 decomposed scheduler `supervise_deploying_stories`; all three audited orchestration hotspots are back under the normal complexity gates.
- **L5 — complete via PR #567:** ARCHITECTURE.md now attributes lifecycle/status/timing to `runs` and engineering token/cost accounting to `engineering_attempt_ledger`.

So the current actionable set is H1 alone: the dispatcher tick with nine ordered steps. Sprint:1457 also pinned CI actions to SHAs, added bounded download retries and a `CI-INFRA-FAILURE` marker the gate carries (PR #582), made the orchestrator's `deploy.yml` wait for the worker-image release of the deployed revision and retry file-only SSH steps (PR #581), and moved the scheduler's PR poller and story completion onto one entered `GitHubAppClient` per operation (PR #580). Sprint:1456 also closed the four follow-up issues sprint:1453 had filed (config defaults now explicit: `DEFAULT_AGENT_TYPE` is required and a GitHub Environment variable, PR #577; the live sweep reads `API_BASE_URL` and reports skipped servers, PR #575) and added a bounded retry to the Claude worker-base installer download (PR #576). Sprint:1454 closed M5 the same day as Phase 1: one card, one PR, a migration with a refuse-if-live guard, a PO archive of the three revoked slot-era rows and a production readback. Sprint:1453 (2026-09-22, cards 1332-1336) closed the audit's Phase 1 in one day; the two follow-ups it filed are issue:11af3167eb3d888181f1 (the sweep's hard-coded `localhost:8000` API address) and issue:e3b4bea6544eac7dfe24 (the inventory skips unmanaged servers), plus issue:6a835ce0d0b4965221a7 (two `shared/config.py` defaults against the new L4 rule) and issue:53c6c4413943e22db0b8 (scaffolder's cached GitHub client on concurrent entry). M9 is complete: both executor CLI private-format boundaries, the lower-level Codex parser behavior and compatibility provenance are explicit and version-bound. The completed work should stay completed; no rewrite is justified.

---

## What changed after the 2026-09-12 refresh

The most architecture-relevant changes since the previous refresh are:

- PRs #487-#490 tightened admission/access, repository-scoped worker credentials, notifications and production resource bounds.
- PR #491 isolated a broken todo task from the rest of the dispatcher cycle and fixed refusal handling where the initiating id is not a Run.
- PRs #492-#495 strengthened worker ownership, transactional parking and temporary-access target/drain semantics.
- PRs #496-#505 added target readiness/finalization evidence and explicit infrastructure recovery.
- PRs #506-#524 substantially expanded central QA contracts, failure classification and worker lifecycle handling.
- PRs #525-#552 expanded deterministic/live acceptance evidence, teardown, generated-product isolation, state-age supervision, operator resume and service-image runtime checks.
- PR #553 removed the production undeploy dependency on `shared.live_harness_cleanup`, moved the shared cleanup primitive to `shared.deployment_cleanup`, and added a service-source import boundary guard.
- PR #554 decomposed server-list reconciliation into typed per-provider-server outcomes plus explicit discovery, missing-server and notification phases, removing the `_sync_server_list` complexity suppression.
- PR #555 decomposed `process_deploy_job` into explicit claim, access-validation, resource-preparation, precheck, execution and typed result-routing phases, removing its C901/PLR0911/PLR0912/PLR0915 suppression while keeping the queue/result and teardown boundaries intact.
- PR #557 decomposed scheduler `supervise_deploying_stories` into thin selection/aggregation, per-story run-state gating and closed `DeployOutcome` routing, removing its C901/PLR0912/PLR0915 suppression and adding structural outcome-coverage guards.
- PR #558 replaced ConfigStore's global unbounded last-known-good fallback with fail-closed defaults plus an explicit 15-minute bounded-stale allowlist for safe scheduler cadence keys, and removed the unused `get_category()` fallback surface.
- PR #559 moved Codex 0.144.6's private `AuthDotJson`/`TokenData`/`AuthMode` and JWT-format mirror into `codex_profile_v01446.py`, left stable reads/locking/health routing in `codex_auth.py`, and added a CI test that requires the worker image's `CODEX_CLI_VERSION` pin to match the adapter version.
- PR #560 pinned the Claude worker image to Claude Code 2.1.278, moved private `.credentials.json` / `claudeAiOauth` field interpretation into `claude_profile_v21278.py`, and added adapter-version and private-format boundary guards.
- PR #562 moved Codex 0.144.6's remaining serde_json 1.0.149 and JWT compatibility into `codex_profile_v01446.py`, left `host_profile.py` vendor-neutral, and added explicit source/version provenance for the pinned Codex and Claude compatibility contracts.
- PR #563 extracted terminal-story and gave-up-attempt worker teardown reconciliation from `task_dispatcher_loop` into an independent scheduler-pipeline worker. The two durable scans now also have sibling failure isolation, so one broken reconciliation pass does not suppress the other or the dispatcher tick.
- PR #572 made the temporary-access sweep's dependency on QA routing a durable, run-specific guard: the sweep cannot escalate or write an incident on a QA run whose story has not consumed its verdict, in either call order, with a contract test for both orders.
- PR #573 moved `supervise_temporary_access` out of `task_dispatcher_loop` into a third scheduler-pipeline loop (`pipeline.py`: `task_dispatcher`, `worker_reconciliation`, `temporary_access`) with its own failure boundary and cycle log; the other ten steps and their comments are unchanged.
- PR #574 removed the scaffolder's process-cached `GitHubAppClient` singleton; each scaffold operation enters `async with GitHubAppClient()` once and closes it on error.
- PR #575 made `scripts/clean_live_tests.py` read the API address from `API_BASE_URL` and made `--inventory` report skipped registered servers, failing distinctly when every server was skipped.
- PR #576 gave the worker-base-claude installer download a bounded retry and a non-empty check before execution.
- PR #577 made `DEFAULT_AGENT_TYPE` required in api, langgraph and telegram_bot (and in Compose), sourced from a GitHub Environment variable in `deploy.yml`, and removed the api's unused Telegram token field.
- PR #578 gave owed owner notifications a durable last-attempt timestamp with an atomic spacing claim, so an attempt is spent at most once per delivery interval regardless of which path (routing or sweep) attempts and in which order; contract tests cover both orders and concurrency.
- PR #579 moved `supervise_owed_owner_notifications` out of `task_dispatcher_loop` into a fourth scheduler-pipeline loop (`owner_notifications`) with its own failure boundary and cycle log.
- PR #580 made `pr_poller.py` and `story_completion.py` enter one `GitHubAppClient` per operation.
- PR #581 made the orchestrator's `deploy.yml` wait (bounded) for the worker-base release marker of the deployed revision and retry file-only SSH steps on connection failure.
- PR #582 pinned third-party actions in `ci.yml` to commit SHAs, added bounded retries to download steps, and made an infrastructure failure produce a greppable `CI-INFRA-FAILURE` marker that the Required CI Gate carries.
- PR #564 added an explicit bounded async lifecycle to `Time4VPSClient` and made scheduler server-sync reuse one `httpx.AsyncClient` across inventory/detail reads in a tick, while preserving one-shot behavior for callers that do not opt into the context. The owned pool closes on both success and provider failure.
- PR #566 added a read-only `--inventory --prefix` mode to `scripts/clean_live_tests.py` (`make test-live-inventory PREFIX=…`), reusing the sweep's collectors and never reaching a clean/delete/fence/recover path, so a retired prefix can be proven empty on production without a destructive run.
- PR #567 rewrote the ARCHITECTURE.md monitoring section around ledger ownership (L5) and narrowed the AGENTS.md environment-variable rule to what `shared/config.py` does (L4).
- PR #568 gave `GitHubAppClientBase` an `async with` lifecycle owning one `httpx.AsyncClient`, closing on error, with per-call clients kept for callers outside the lifecycle and no global singleton.
- PR #569 made `EmbeddingClient` share one `httpx.AsyncClient` across the batches of one generate call.
- PR #570 removed `Contour.legacy` and the `mega-test` prefix from `shared/live_contour.py` after the PO's recorded production inventory found zero matches on every surface; the contour tests were rewritten to the two-prefix contour.

These changes mostly harden correctness. H1 remains because scheduler-pipeline still owns one ordered multi-responsibility routing/supervision cycle, although PR #563 removed worker teardown reconciliation from that positional boundary. M10 is complete: the server-sync, LangGraph deploy-consumer and scheduler deploy-supervisor hotspots all use bounded routing phases without local complexity suppressions. PR #553 closed the M2 production-to-harness dependency, PR #558 closed M7 by making stale config use explicit, bounded and key-classified, PR #559 made the Codex private-format boundary explicit, PR #560 did the same for Claude, and PR #562 closed M9 by moving the remaining Codex parser/JWT compatibility behind the versioned adapter and pinning compatibility provenance.

---

# Current finding matrix

| ID | Severity | Status on 2026-09-22 | Current conclusion |
|---|---|---|---|
| H1 | High | Partial; three responsibilities extracted | Worker teardown reconciliation (PR #563), temporary-access cleanup (PRs #572/#573) and owed owner-notification delivery (PRs #578/#579) run as their own loops on durable facts; the dispatcher tick still has nine ordered routing/supervision steps. |
| H2 | High | Complete | System config is canonical for PO summarization tuning; retired numeric env plumbing remains absent. |
| H3 | High | Complete | GitHub expected failures remain status-driven rather than exception-string/empty-result fallbacks. |
| H4 | High | Complete | Worker services remain under the root Ruff policy. |
| M1 | Medium | Complete | The old worker-wrapper private task ownership split is gone. |
| M2 | Medium | Complete | PR #553 moved the undeploy/recovery primitive to shared.deployment_cleanup and added a guard forbidding production service imports from shared.live_harness*. |
| M3 | Medium | Complete | Production PO startup requires durable checkpoint configuration; MemorySaver remains an explicit graph/test-capable path rather than the enabled production downgrade. |
| M4 | Medium | Complete | Run accounting compatibility is gone and the engineering attempt ledger is canonical. |
| M5 | Medium | Complete via PR #571 | `env_key`/`subject` dropped, both target columns NOT NULL, the three revoked target-less rows archived and deleted, no legacy branch in the router; drain remains only for escalated target-backed `revoke_failed` rows. |
| M6 | Medium | Complete | PO tools use owner modules; retired compatibility re-exports remain removed. |
| M7 | Medium | Complete | PR #558 made stale reads opt-in and bounded; unclassified config fails closed and only a small scheduler cadence allowlist may use a 15-minute last-known value. |
| M8 | Medium | Complete | Frontend images use plain npm ci; legacy-peer-deps is absent. |
| M9 | Medium | Complete via PRs #559/#560/#562 | Codex 0.144.6 owns its private serde_json/JWT compatibility behind the versioned adapter, Claude Code 2.1.278 has the corresponding credential adapter, and both pins have explicit provenance and upgrade gates. |
| M10 | Medium | Complete | PRs #554, #555 and #557 decomposed all three audited orchestration hotspots; server-sync, LangGraph deploy consumption and scheduler deploy supervision no longer carry the relevant complexity suppressions. |
| L1 | Low | Complete | Retired live-test Makefile entrypoints remain removed; only regression comments/guards name them. |
| L2 | Low | Complete via PRs #566/#570 | Read-only production inventory recorded zero `mega-test` matches; `Contour.legacy` and the prefix are gone. |
| L3 | Low | Effectively closed (PRs #564/#568/#569/#574) | Time4VPS sync, the GitHub App client, the embedding client and the scaffolder own bounded pools; what remains is deliberate one-shot probes. |
| L4 | Low | Complete via PR #567 | AGENTS.md states the narrowed rule; two `shared/config.py` defaults that now contradict it are tracked as issue:6a835ce0d0b4965221a7. |
| L5 | Low | Complete via PR #567 | ARCHITECTURE.md attributes token/cost accounting to engineering_attempt_ledger. |

K1-K4 below remain retention notes, not additional deletion tasks.

---

# High severity

## H1. Scheduler process split is complete; ordered pipeline ownership is not

**Severity:** High  
**Removal safety:** 2/5  
**Removal simplicity:** 1/5  
**Status:** Partially complete, still current.

### Current evidence

PR #485 correctly replaced the aggregate scheduler process with:

- scheduler-pipeline;
- scheduler-infrastructure;
- scheduler-maintenance.

That part of the original finding is closed.

The remaining scheduler-pipeline entrypoint still runs services/scheduler/src/tasks/task_dispatcher.py::task_dispatcher_loop. On current main the loop performs, in one ordered tick:

1. scaffold triggering;
2. engineering dispatch admission/publication;
3. story completion;
4. merged-PR handling and CI failure routing;
5. stuck-story/task and failed-task supervision;
6. resource/deploy/user-secret supervision;
7. ~~owed owner-notification recovery~~ (moved out by PRs #578/#579, sprint:1457);
8. QA/testing supervision;
9. state-age watchdogs;
10. stage notices;
11. ~~temporary-access cleanup~~ (moved out by PRs #572/#573, sprint:1456).

PR #563 moved terminal-story and gave-up-attempt worker reconciliation out of this tick. `scheduler-pipeline` now starts a sibling `worker_reconciliation` loop on the same cadence; each of its two durable scans has a contained failure boundary, and the dispatcher no longer waits for Redis worker scans.

The code comments still document ordering as correctness, not just an implementation preference. In particular, owed notifications are swept before routing that can create new notices, QA routing happens before access cleanup, and stage notices run after state-moving supervisors.

The remaining routing/supervision tick stays inside one broad `dispatcher_cycle_error` boundary. Worker teardown reconciliation, temporary-access cleanup and owed owner-notification delivery are no longer inside it: `pipeline.py` now starts four loops (`task_dispatcher`, `worker_reconciliation`, `temporary_access`, `owner_notifications`), and production logs after the 2026-09-23T10:20Z deploy show all four cycling independently.

PRs #572/#573 are the template for every further slice: first the ordering edge became a durable guard on the record (the sweep checks, per QA run, that the story has consumed the verdict, in any call order, with a test for both orders), and only then did the call leave the tick.

### What improved since 2026-09-12

PR #491 prevents one broken todo task from aborting dispatch of all later todo tasks. Later work added transactional parks, worker reconciliation, state-age supervision and stage notices. These are correctness wins.

PR #563 is the first H1 extraction after the process split: worker teardown reconciliation now consumes only durable terminal/settled-attempt facts in a sibling loop, and a failure in one teardown scan does not suppress the other. This removes one independent recovery workflow from the positional dispatcher cycle without changing teardown business logic.

### Why it still matters

A process split is only part of the boundary. The remaining dispatcher cycle still has:

- one clock;
- one process lifecycle shared with the sibling reconciliation worker;
- ordering encoded by call order and comments for several routing/supervision edges;
- latency coupling between unrelated supervisors;
- a broad dispatcher-cycle failure boundary;
- difficult independent scaling and recovery semantics for the responsibilities still inside that tick.

Blindly turning every call into its own loop would be unsafe because some orderings are real contracts.

### Recommendation

Do not start with another mechanical process split.

First turn the required ordering into explicit durable facts and tests. A useful decomposition boundary is:

1. dispatch / PR lifecycle;
2. story-state supervision;
3. ~~owed notification delivery~~ (done);
4. QA handoff and verdict routing;
5. ~~temporary-access cleanup~~ (done).

For every edge that currently depends on call order, define the durable precondition that makes it safe to run independently. Then split one responsibility at a time.

---

# Medium severity

## M5. Legacy temporary-access schema and runtime branches are gone

**Severity:** Medium  
**Status:** Complete via PR #571 (sprint:1454, migration 4d8e1f2a3b5c), deployed to production 2026-09-22T19:00Z.

The owner chose full removal over keeping revoked slot-era history: production held exactly three target-less rows, all revoked in 2026-08 (two by `run_terminal`, one by a manual drain on 2026-09-12), and no supported producer could create another because `TemporaryAccessGrantCreate` already required both target fields.

What PR #571 did, in one reviewed step:

- alembic revision 4d8e1f2a3b5c refuses to run if any target-less row is not `revoked`, deletes the revoked target-less rows, makes `target_application_id` and `target_base_url` NOT NULL and drops `env_key` and `subject`; the downgrade restores nullability only;
- `shared/models/temporary_access_grant.py` has no `legacy_env_key`/`legacy_subject` and non-optional targets;
- `services/api/src/routers/temporary_access.py` has no `_is_legacy`, `_reject_legacy_record`, `_LEGACY_REMEDIATION`, no target-less filter in the list route; `POST /{grant_id}/drain` accepts only a complete target-backed row stamped `revoke_failed` and escalated by the reconciler, with the same audit row;
- the service tests build only current-lifecycle rows and include a real-Postgres migration test; `docs/CONTRACTS.md` describes one lifecycle.

Operational record on sprint:1454: the PO archived the three rows to the production backup location before the merge (`[po:archive]`), the owner agreed to the deploy (`[po:deploy-ok]`), and the readback after deploy run 35768804207 shows alembic 4d8e1f2a3b5c, the two columns absent, both targets `is_nullable = NO`, two rows and zero target-less (`[po:readback]`).

### Retention guidance

The operator drain survives for its remaining purpose (escalated target-backed revoke failures). K1-K4 are unaffected.

---

## M7. ConfigStore stale reads are explicit, bounded and key-classified

**Severity:** Medium  
**Removal safety:** 2/5  
**Removal simplicity:** 3/5  
**Status:** Complete via PR #558.

### Current evidence

PR #558 changed `shared/config_store.py` so a key with no stale policy fails closed when the
config API is unreachable, returns malformed data, or answers a non-404 error. A caller must now
opt an exact key into `BoundedStalePolicy` to use a last-known value.

The cache records when the value was fetched separately from its ordinary cache TTL. On source
failure, an opted-in stale value is accepted only while its age is within the configured maximum;
after that bound, `ConfigStoreUnavailableError` is raised. Structured warning/error events include
the stale age and maximum stale age.

Scheduler startup owns the current stale-safe classification. The 15-minute allowlist is limited to:

- scheduler dispatch interval;
- GitHub sync interval;
- server-list sync interval;
- server-details sync interval;
- RAG summarizer poll interval;
- metrics cleanup interval.

Deploy retry budgets, supervisor limits, health thresholds and every unlisted/new key inherit the
fail-closed default automatically.

The unused `get_category()` API and its empty-mapping-on-failure behavior were removed. A 404
continues to mean a missing key rather than source unavailability.

### Validation

Regression coverage now proves:

- unclassified cached keys fail closed on source loss;
- bounded keys survive network, 5xx and malformed-body failures only inside their age budget;
- the exact age boundary is honored and expired stale data is refused;
- deleted keys still surface as missing even when an old value was cached;
- scheduler startup passes stale policy only for the explicit safe allowlist;
- behavior-changing deploy policy remains fail closed.

PR #558 passed the full required CI gate: Ruff formatting/lint, unit tests, offline live
regressions, scheduler/API/LangGraph/infra service tests, integration suites, template
compatibility and production service-image entrypoint imports. It merged as
`acd44c68aed61a3c1d0383ab826a4ab3b63c8eea`.

### Retention guidance

Keep fail-closed as the ConfigStore default. Add a bounded stale policy only for a reviewed key
whose semantics remain safe during a short source outage; do not broaden the scheduler allowlist by
category or prefix.

---

## M9. Executor-profile diagnostics mirror private CLI file formats

**Severity:** Medium  
**Removal safety:** 2/5  
**Removal simplicity:** 2/5  
**Status:** Complete via PRs #559, #560 and #562.

### What PR #559 completed

The Codex worker image pins `@openai/codex` to `0.144.6`. PR #559 made the matching high-level profile contract explicit:

- `services/worker-manager/src/codex_profile_v01446.py` became the named compatibility adapter for Codex 0.144.6 and owns the private `AuthDotJson` / `TokenData` / `AuthMode` shape, JWT claim compatibility and pinned format refusals.
- `services/worker-manager/src/codex_auth.py` kept non-format responsibilities: stable file reads, advisory locking, file permissions, config checks, metadata interpretation and health classification.
- a regression test requires the worker image's `CODEX_CLI_VERSION` pin to equal the adapter version, so a future Codex bump cannot silently reuse the old adapter.
- the admission/diagnostic compatibility matrix continued to cover malformed/accepted fields, auth-mode precedence, JWT claims, serde_json edge cases, lock races and credential-safe refusal behavior through the public reader.

PR #559 merged as `a741e2901e20c0395db701428f948aba6b781fff`.

### What PR #560 completed

The Claude worker image pins Claude Code to `2.1.278`, asks the official installer for that exact release and checks `claude --version` during the image build.

`services/worker-manager/src/claude_profile_v21278.py` is the named compatibility adapter for the private `.credentials.json` / `claudeAiOauth` shape, including `accessToken`, `refreshToken` and `expiresAt`. `claude_auth.py` keeps stable file reads, standard-JSON totality, health classification and admission semantics instead of vendor-format field knowledge.

Regression coverage requires the Dockerfile's `CLAUDE_CODE_VERSION` to equal the adapter version and keeps the private field names out of the stable reader.

PR #560 merged as `144b50cd5938bbb27d974ce58942c479726e361b`.

### What PR #562 completed

PR #562 closed the remaining parser/provenance residue:

- `services/worker-manager/src/host_profile.py` is vendor-neutral again: it owns bounded standard-JSON parsing and shared metadata/inspection helpers, not Codex serde_json or JWT behavior.
- `services/worker-manager/src/codex_profile_v01446.py` now owns the pinned serde_json 1.0.149 duplicate-key, surrogate, numeric-range and nesting semantics plus the Codex-only JWT expiry parser.
- `services/worker-manager/tests/fixtures/profile_adapter_provenance.json` binds the Codex contract to CLI 0.144.6, upstream `rust-v0.144.6`, source commit `5d1fbf26c43abc65a203928b2e31561cb039e06d`, serde_json 1.0.149 and the audited source paths. The same manifest records the pinned installed-observation provenance for Claude Code 2.1.278.
- structural tests keep Codex parser primitives and JWT parsing out of the generic host-profile layer, while the existing admission/diagnostic matrix continues to exercise the public behavior.

The TDD red commit failed the unit suite in CI run #2062 as intended. The final CI run #2073 passed the Required CI Gate, including Ruff, the full unit/offline regressions, worker-manager service tests, template compatibility and production service-image entrypoint imports.

PR #562 merged as `45b0cb9f5c5748ab080a554a30026342c8a09b3a`.

### Retention guidance

Keep `host_profile.py` vendor-neutral. A CLI version bump should update the worker-image pin, its versioned adapter and the provenance contract in the same change. Keep compatibility fixtures synthetic and credential-safe.

If either vendor later exposes an official introspection API that proves the same fail-closed facts, it can replace the private-format mirror. Do not trade the current explicit boundary for permissive parsing.

---

## M10. Core orchestration complexity exemptions are removed

**Severity:** Medium  
**Removal safety:** 3/5  
**Removal simplicity:** 2/5  
**Status:** Complete via PRs #554, #555 and #557.

### Current evidence

PR #554 completed the server-sync hotspot. `_sync_server_list` aggregates a closed typed
`ProviderServerOutcome` from one-server reconciliation and delegates discovery, missing-server
marking and managed-server notifications to explicit phases. Its C901/PLR0912/PLR0915 suppression
is gone.

PR #555 completed the LangGraph deploy-consumer hotspot. `process_deploy_job` delegates to
explicit claim, access-validation, resource-preparation, precheck, execution and typed
result-routing phases. Its C901/PLR0911/PLR0912/PLR0915 suppression is gone, and a structural
regression test keeps the entrypoint thin and suppression-free.

PR #557 completed the scheduler deploy-supervisor hotspot. `supervise_deploying_stories` now owns
only story selection and aggregate counting. Per-story run-state gating lives in
`_supervise_deploying_story`, terminal outcome routing lives in `_route_deploy_outcome`, and one
`DeploySupervisorAction` represents the externally visible effect of one story in one tick.

The scheduler regression coverage now asserts that the public supervisor entrypoint stays free of
local complexity suppression and that `_ROUTED_DEPLOY_OUTCOMES == frozenset(DeployOutcome)`, so a
new deploy outcome cannot silently arrive without a supervisor route.

None of the three orchestration hotspots identified by M10 now carries the complexity/branch/
statement suppressions that motivated this finding.

### What the completed sequence proved

The three refactors used the same boundary pattern without changing ownership semantics:

- keep the public orchestration entrypoint;
- turn one unit of work into a closed typed outcome/action;
- delegate side effects to existing focused handlers;
- aggregate or route only after the typed boundary;
- keep a structural regression test around the new seam.

Existing server discovery/allowlist behavior, deploy queue/result and teardown contracts, story
transitions, retry budgets, infrastructure refusal handling, durable owner notifications,
user-secret waits and QA handoff behavior remain covered by unit, service and integration tests.

All three PRs passed the full required CI gate, including Ruff, unit tests, offline live
regressions, applicable service/integration tests, template compatibility and production
service-image entrypoint imports. PR #554 merged as
`b13d5976ffc88a3b172ff2e4e00481b16d408223`; PR #555 merged as
`3e63bfb30e47b04a1087f377c21f1b6cb6a63fb8`; PR #557 merged as
`798e06a66a2d7de982af8e0e17d6efcd198f3869`.

### Retention guidance

Keep these typed routing seams and structural guards. If one of the orchestration entrypoints grows
again, split by state transition or outcome rather than reintroducing local complexity suppressions.
This finding needs no further cleanup task.

---

# Low severity

## L2. Legacy live-test prefix mega-test is retired

**Severity:** Low  
**Status:** Complete via PRs #566 and #570 (sprint:1453).

PR #566 added `make test-live-inventory PREFIX=<prefix>`: a read-only mode of `scripts/clean_live_tests.py` over every surface the sweep covers (database projects, GitHub organisation repositories, deployed stacks on registered servers, Redis capability work and worker metadata, local Docker containers, local workspaces). It exits 0 only when every surface was readable and nothing matched.

The PO ran it on the production host on 2026-09-22 from a separate clone of main (the deployed checkout is bind-mounted into running services and was not touched): exit 0, zero matches on every surface. The record is the `[po:inventory]` comment on sprint:1453. PR #570 then removed `Contour.legacy` and the `mega-test` entry; `git grep -n mega-test` on main is empty.

Two weaknesses of that proof are filed rather than hidden: the script hard-codes `CLEANUP_API_URL = http://localhost:8000`, which production does not publish, so the run needed a temporary loopback proxy (issue:11af3167eb3d888181f1); and `deployed_stacks` skipped all eight registered servers as not managed cleanup targets, so the surface was "readable" without any remote host being scanned (issue:e3b4bea6544eac7dfe24).

---

## L3. External clients own bounded HTTP pools where an operation has a lifecycle

**Severity:** Low  
**Removal safety:** 4/5  
**Removal simplicity:** 3/5  
**Status:** Open for caller adoption only; three bounded slices complete (PRs #564, #568, #569).

Completed slices:

- PR #564: `Time4VPSClient` has an explicit async context owning one `httpx.AsyncClient`; scheduler server-sync enters it only around provider I/O; one-shot callers are unchanged.
- PR #568: `GitHubAppClientBase` gained `__aenter__`/`__aexit__` owning one `httpx.AsyncClient`; every `_make_request` inside the lifecycle reuses it, the pool closes on the failure path, nested entry is refused, and callers outside the lifecycle keep a per-call client. No process-global singleton was introduced and the rate-limit retry is unchanged. Today only `services/langgraph/src/subgraphs/devops/env_contract_loader.py` enters the lifecycle.
- PR #569: `EmbeddingClient` shares one `httpx.AsyncClient(timeout=self.timeout)` across all batches of one generate call, closing on success and failure.

What remains, and is deliberately not a cleanup task:

- callers of `GitHubAppClient` that run several requests per operation (scaffolder consumer, story completion, PR polling) have not yet adopted `async with GitHubAppClient()`; adopt it where an operation already has a clear boundary, one caller at a time;
- the scaffolder's process-cached client (`services/scaffolder/src/clients/github.py::get_github_client`) would share and lose its pool on concurrent entry; no caller does that today, tracked as issue:53c6c4413943e22db0b8;
- `services/infra-service/src/provisioner/bitlaunch.py::get_server_ip()`, `shared/clients/registry.py::manifest_digest()` and the health probe in `shared/clients/infra_client.py` are one-shot probes where pooling would add lifecycle complexity for nothing; leave them.

---

## L4. Environment-default convention matches the code

**Severity:** Low  
**Status:** Complete via PR #567 (sprint:1453).

AGENTS.md now states the narrowed rule: identity, credentials, connectivity and required production policy have no default (`Field(...)` or a `*_field()` helper with `required=True`); safe presentation, logging and local-ergonomics settings may carry a documented default, as `service_name`, `log_format` and `log_level` do; behaviour-changing production policy is explicit configuration, never a hidden fallback. `shared/config.py` was not changed to make the rule true.

The rule now exposes two defaults that were previously hidden behind the blanket wording: `default_agent_type_field()` defaults to `"claude"` (behaviour-changing production policy) and `telegram_token_field(required=False)` defaults to `""` (a credential). They are tracked as issue:6a835ce0d0b4965221a7 for the owner to decide between making them required and documenting an exception.

---

## L5. Architecture documentation describes ledger accounting

**Severity:** Low  
**Status:** Complete via PR #567 (sprint:1453).

The ARCHITECTURE.md monitoring section now says that `runs` owns lifecycle, status, timing and result identity, that `engineering_attempt_ledger` owns engineering token and cost accounting, and that Grafana reads engineering accounting from the ledger. No repository document attributes tokens or cost to `runs` any more.

---

# Completed original findings

These findings were spot-checked during the 2026-09-21 refresh and show no regression evidence.

## H2. PO summarization configuration

**Complete — PRs #476 and #478.**

The PO consumer still constructs ConfigStore and validates the required summarization keys. Search finds no return of the retired numeric env plumbing.

## H3. GitHub repository failure semantics

**Complete — PR #477.**

Expected repository conditions remain typed/status-driven. Keep the narrow deletion race fallback described in K3.

## H4. Worker root lint policy

**Complete — PR #482, after #475.**

The worker services remain on the repository Ruff policy. M10 is a distinct issue about local complexity suppressions in orchestration functions, not a regression of H4.

## M1. Worker-wrapper run task ownership

**Complete — PR #463.**

The old private task/stop-event ownership hack remains removed.

## M2. Runtime and live-harness cleanup ownership

**Complete — PR #553.**

The production undeploy path now imports the shared cleanup command/script contract from
`shared.deployment_cleanup`, while the live harness consumes that same neutral primitive.
The cleanup policy itself was not duplicated or rewritten.

A repository boundary test now scans `services/*/src/**/*.py` and fails if production
service code imports `shared.live_harness*`. Production-facing cleanup tests use the
neutral module as well.

**Validation:** the full required CI gate passed, including Ruff, unit tests, offline live
regressions, LangGraph service tests, integration tests, shared freshness and production
service-image entrypoint imports. PR #553 merged as
`44e330a132a394f44229eec8731794a656a12927`.

The large harness module can still be decomposed internally when useful, but the
production-to-harness dependency identified by M2 is closed.

## M3. Durable PO checkpointer

**Complete — PR #464.**

The reusable graph builder can still construct MemorySaver when no checkpoint URL is supplied, which is useful for explicit tests/standalone construction. Enabled production PO startup requires its durable checkpoint configuration, so this is not the old silent production downgrade.

## M4. Run accounting compatibility

**Complete — PR #483.**

The old projection helper is absent from production code; its name appears only in a regression/boundary test. Engineering accounting belongs to the ledger.

## M6. PO tool compatibility re-exports

**Complete — PRs #479 and #480.**

No new compatibility facade was found in the refresh.

## M8. Frontend legacy peer dependency bypass

**Complete — PR #481.**

Search finds no legacy-peer-deps use.

## L1. Retired live-test Makefile entrypoints

**Complete — PR #486.**

The Makefile does not define the retired targets. Their spellings remain only in comments/guards documenting that they are forbidden.

---

# Legacy-looking code that should not be removed blindly

## K1. Legacy recipient user_id tombstone

**Keep for now.**

The special rejection protects a historically ambiguous field from being silently ignored. Remove it only after producer/version/Redis-retention proof shows the old payload cannot appear.

## K2. CLAUDE.md compatibility entrypoint

**Keep while the external tool discovers repository instructions through it.**

It points to the canonical instructions rather than duplicating policy, so its maintenance cost is tiny.

## K3. Repo-token to org-token fallback during repository deletion

**Keep.**

This remains a narrow, status-driven workaround for repository-installation visibility after creation. It is qualitatively different from a broad exception fallback.

## K4. ConfigStore last-known-good

**Keep selectively, not globally.**

PR #558 now enforces this split: stale use is exact-key opt-in with a maximum age, while every unlisted key fails closed. Keep that default and review any future allowlist addition narrowly.

---

# Suggested cleanup order from current main

## Phase 1 — safe, local cleanup

Complete. Sprint:1453 (2026-09-22, PRs #566-#570) closed L5, L4 and L2 and did the two remaining long-lived/batched L3 slices. What is left of L3 is caller adoption of the GitHub client lifecycle, which belongs to whoever next touches those callers, not to a cleanup card.

## Phase 2 — bounded compatibility cleanup

Complete. Sprint:1454 (2026-09-22, PR #571) retired the legacy temporary-access lifecycle as a migration with proof; nothing else of the audit needs a compatibility cleanup.

## Phase 3 — scheduler boundary decomposition

1. **H1, done so far:** PR #563 (worker teardown reconciliation), PRs #572/#573 (temporary-access cleanup, sprint:1456) and PRs #578/#579 (owed owner-notification delivery, sprint:1457) each turned one ordering edge into a durable fact and then moved one responsibility into its own scheduler-pipeline loop.
2. **H1, next slices, one per sprint:** story-state supervision (the state-age watchdog and stage notices, whose edge is "after every state-moving supervisor": it becomes a compare-and-set on the story's durable state and anchor at the moment the watchdog acts or the notice is sent), then QA handoff and verdict routing, leaving dispatch / PR lifecycle as the tick's core.
3. Repeat only where tests prove the new boundary preserves at-least-once/retry/notification behavior.

H1 is the only open finding; it should still be approached one responsibility at a time rather than as a scheduler rewrite.

---

# Overall assessment

The repository is healthier than the original audit snapshot. A large amount of transition residue has been removed, and recent work consistently moves failure handling toward typed outcomes, durable evidence and explicit recovery.

The remaining debt is concentrated rather than diffuse:

- **coordination concentration:** scheduler-pipeline still has an ordered routing/supervision cycle of nine steps, though worker teardown reconciliation, temporary-access cleanup and owner-notification delivery are now independent loops;
- **harness concentration:** live-harness remains oversized, but PR #553 removed the production import dependency and pinned that boundary;
- **compatibility residue:** none left; the temporary-access legacy rows and schema are gone (sprint:1454);
- **small hygiene debt:** none open; the scaffolder adopted the GitHub client lifecycle, the config defaults are explicit and the live sweep reports honestly (sprint:1456).

Executor private-format coupling is no longer an open finding: the pinned Codex and Claude contracts now have explicit versioned adapters, upgrade gates and provenance.

The next cleanup should continue the repository's existing direction: preserve typed contracts and durable evidence, then make ownership boundaries match them. The evidence does not support a rewrite.
