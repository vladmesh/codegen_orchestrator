# Astra architecture audit

Date: 2026-09-21  
Audited branch: main at 145d65e5070f121c79e5c9af48bab8294d9c81e2 (after PR #552)  
Previous refresh: 2026-09-12, through f9ac3eb8b8137ee7dcbb5b976946e4c12e1b8c9f  
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

Those improvements do not invalidate the main architectural concern from the original audit. The scheduler process split from PR #485 remains useful, but scheduler-pipeline still owns one large ordered cycle whose sequencing is part of the product contract. That cycle has grown since the last refresh.

### Current status

Of the original sixteen H/M/L findings:

- **9 remain complete:** H2, H3, H4, M1, M3, M4, M6, M8 and L1.
- **H1 remains partially complete.**
- **6 remain open:** M2, M5, M7 and L2-L4.

This refresh adds three findings:

- **M9 — new since the old audit:** executor-profile health now mirrors private Codex/Claude credential formats closely enough that updating a pinned CLI carries adapter-maintenance risk.
- **M10 — newly identified, but not newly introduced:** several core orchestration functions are explicitly exempt from Ruff complexity/branch/statement limits.
- **L5 — newly identified documentation drift:** ARCHITECTURE.md still says Run rows hold engineering token/cost accounting even though M4 made engineering_attempt_ledger canonical.

So the current actionable set is one High finding, five Medium findings and four Low findings. The completed original work should stay completed; no rewrite is justified.

---

## What changed after the 2026-09-12 refresh

The most architecture-relevant changes since the previous refresh are:

- PRs #487-#490 tightened admission/access, repository-scoped worker credentials, notifications and production resource bounds.
- PR #491 isolated a broken todo task from the rest of the dispatcher cycle and fixed refusal handling where the initiating id is not a Run.
- PRs #492-#495 strengthened worker ownership, transactional parking and temporary-access target/drain semantics.
- PRs #496-#505 added target readiness/finalization evidence and explicit infrastructure recovery.
- PRs #506-#524 substantially expanded central QA contracts, failure classification and worker lifecycle handling.
- PRs #525-#552 expanded deterministic/live acceptance evidence, teardown, generated-product isolation, state-age supervision, operator resume and service-image runtime checks.

These changes mostly harden correctness. They also make a few existing coordination modules larger, which is why H1, M2 and M10 deserve attention even though many individual failure cases are better than before.

---

# Current finding matrix

| ID | Severity | Status on 2026-09-21 | Current conclusion |
|---|---|---|---|
| H1 | High | Partial | Three scheduler services exist, but scheduler-pipeline still has one ordered dispatcher/supervisor/reconciliation cycle and one cycle failure boundary. |
| H2 | High | Complete | System config is canonical for PO summarization tuning; retired numeric env plumbing remains absent. |
| H3 | High | Complete | GitHub expected failures remain status-driven rather than exception-string/empty-result fallbacks. |
| H4 | High | Complete | Worker services remain under the root Ruff policy. |
| M1 | Medium | Complete | The old worker-wrapper private task ownership split is gone. |
| M2 | Medium | Open; worsened | Production deploy lifecycle still imports shared.live_harness_cleanup, which has grown into a large live-harness/runtime utility module. |
| M3 | Medium | Complete | Production PO startup requires durable checkpoint configuration; MemorySaver remains an explicit graph/test-capable path rather than the enabled production downgrade. |
| M4 | Medium | Complete | Run accounting compatibility is gone and the engineering attempt ledger is canonical. |
| M5 | Medium | Open; safer remediation | Legacy temporary-access columns/branches remain, but operator drain now provides a supported path to eliminate unreconcilable live legacy rows. |
| M6 | Medium | Complete | PO tools use owner modules; retired compatibility re-exports remain removed. |
| M7 | Medium | Open | ConfigStore still serves an unbounded last-known value after source failure; policy is global rather than key-classified. |
| M8 | Medium | Complete | Frontend images use plain npm ci; legacy-peer-deps is absent. |
| M9 | Medium | New | Worker-manager mirrors detailed private executor credential/profile formats, especially Codex auth.json/serde_json behavior. |
| M10 | Medium | New to audit | Core deploy/server-sync orchestration still opts out of complexity, branch and statement limits at function scope. |
| L1 | Low | Complete | Retired live-test Makefile entrypoints remain removed; only regression comments/guards name them. |
| L2 | Low | Open | The production live contour still sweeps the legacy mega-test prefix. |
| L3 | Low | Open | Several long-lived external client classes still instantiate a fresh httpx.AsyncClient per request/batch. |
| L4 | Low | Open | AGENTS.md says environment variables never use defaults while shared/config.py intentionally defines safe defaults. |
| L5 | Low | New | ARCHITECTURE.md still describes retired Run token/cost storage instead of the engineering attempt ledger. |

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

The remaining scheduler-pipeline entrypoint still runs services/scheduler/src/tasks/task_dispatcher.py::task_dispatcher_loop. On current main that module is about 594 lines and the loop performs, in one ordered tick:

1. scaffold triggering;
2. engineering dispatch admission/publication;
3. story completion;
4. merged-PR handling and CI failure routing;
5. stuck-story/task and failed-task supervision;
6. resource/deploy/user-secret supervision;
7. owed owner-notification recovery;
8. QA/testing supervision;
9. state-age watchdogs;
10. stage notices;
11. temporary-access cleanup;
12. terminal and gave-up worker reconciliation.

The code comments still document ordering as correctness, not just an implementation preference. In particular, owed notifications are swept before routing that can create new notices, QA routing happens before access cleanup, and stage notices run after state-moving supervisors.

The whole tick remains inside one broad dispatcher_cycle_error boundary.

### What improved since 2026-09-12

PR #491 prevents one broken todo task from aborting dispatch of all later todo tasks. Later work added transactional parks, worker reconciliation, state-age supervision and stage notices. These are correctness wins.

They also increased the number of independently meaningful workflows inside the same periodic cycle. task_dispatcher.py grew from roughly 21.6 KB at the previous refresh to roughly 27.1 KB now.

### Why it still matters

A process split is only part of the boundary. The remaining cycle still has:

- one clock;
- one process lifecycle;
- ordering encoded by call order and comments;
- latency coupling between unrelated supervisors;
- a broad cycle-level failure boundary;
- difficult independent scaling and recovery semantics.

Blindly turning every call into its own loop would be unsafe because some orderings are real contracts.

### Recommendation

Do not start with another mechanical process split.

First turn the required ordering into explicit durable facts and tests. A useful decomposition boundary is:

1. dispatch / PR lifecycle;
2. story-state supervision;
3. owed notification delivery;
4. QA handoff and verdict routing;
5. temporary-access and worker cleanup.

For every edge that currently depends on call order, define the durable precondition that makes it safe to run independently. Then split one responsibility at a time.

---

# Medium severity

## M2. Runtime and live-harness ownership still leak through shared, and the leak grew

**Severity:** Medium  
**Removal safety:** 3/5  
**Removal simplicity:** 2/5  
**Status:** Open and more important than in the previous audit.

### Current evidence

services/langgraph/src/consumers/deploy_lifecycle.py still imports:

- REMOTE_CLEANUP_SCRIPT;
- build_remote_cleanup_command;

from shared.live_harness_cleanup.

That import is on the production undeploy path.

At the same time shared/live_harness_cleanup.py is now roughly 1,230 lines / 52 KB, up from about 36 KB at the previous refresh. It contains a broad mix of concerns: live-run cleanup, GitHub probes, registry cleanup/probes, target selection and acceptance evidence.

The rest of the module is heavily used by tests/live and cleanup scripts, which makes the production import direction backwards: runtime depends on a module whose dominant owner is the harness.

The 2026-09-21 runtime-image fix is related evidence of why ambient shared dependencies matter: the scaffolder image had to add aiohttp for a shared notification import, and CI now imports every production service entrypoint from its built image to catch undeclared transitive dependencies.

### Why it matters

A shared package is useful for stable contracts and small runtime primitives. It becomes dangerous when it is also the home of test-harness orchestration:

- runtime images can acquire dependencies because a harness module grew;
- test-only refactors can change production importability;
- ownership is unclear;
- a large module makes it hard to know what is safe to import into a service image.

### Recommendation

Extract only the production-owned undeploy primitive first, for example a small shared/runtime cleanup module containing the remote cleanup script/command contract.

Then move live acceptance/probe/sweep code under a harness-owned package or tests/live support package. Add an import-boundary test that production service modules cannot import harness-owned modules.

Do not move all 1,230 lines in one PR.

---

## M5. Legacy temporary-access schema and runtime branches still exist

**Severity:** Medium  
**Removal safety:** 3/5  
**Removal simplicity:** 3/5  
**Status:** Open, but the safe removal path improved.

### Current evidence

shared/models/temporary_access_grant.py still maps the retired columns as:

- legacy_env_key -> env_key;
- legacy_subject -> subject.

services/api/src/routers/temporary_access.py still contains:

- _LEGACY_REMEDIATION;
- _is_legacy();
- _reject_legacy_record();
- special behavior for target-less legacy rows.

This is not dead code: a live target-less row still changes API behavior.

### What changed since the old audit

PR #495 narrowed contention to an exact known target and added an audited operator drain. The drain can close either a live legacy row or a properly escalated target-backed revoke failure without manual SQL.

That makes this finding less risky to finish: the repository now has a supported operational escape hatch.

### Recommendation

Treat this as a migration with proof:

1. use the drain path for any remaining live legacy rows;
2. prove no supported producer can create target-less rows;
3. decide whether revoked historical rows need to stay queryable;
4. migrate/archive history if required;
5. remove legacy runtime branches;
6. drop env_key and subject in a final schema migration.

The operator drain should survive if it still has a purpose for current target-backed revoke failures; only its legacy branch should disappear.

---

## M7. ConfigStore still has a global, unbounded last-known-good policy

**Severity:** Medium  
**Removal safety:** 2/5  
**Removal simplicity:** 3/5  
**Status:** Open.

### Current evidence

shared/config_store.py::_source_unavailable() returns the last cached value whenever the config API is unreachable, returns malformed data, or answers a non-404 error.

Once a key has been cached, this fallback has no maximum stale age. The normal cache TTL decides when to re-read, but it does not bound how old a value may be when the source is broken.

The same module also has get_category(), which returns an empty mapping after a request failure. Current code search finds no production caller for get_category(), so that branch is currently mostly dead API surface rather than a production fallback.

### Why it matters

The repository now has more security, budget, executor and admission policy in system config. Treating every key as equally safe to run stale is too coarse.

A stale polling interval is different from stale admission/security policy.

### Recommendation

Classify config at the call site or schema level:

- startup-critical / security / admission / budget: fail closed;
- operational timing / observability: bounded last-known-good may be acceptable.

If stale reads are retained, give them a maximum staleness bound and a metric/alert. Remove get_category() if no production caller appears.

This is a policy split, not a request to globally delete resilience.

---

## M9. Executor-profile diagnostics mirror private CLI file formats

**Severity:** Medium  
**Removal safety:** 2/5  
**Removal simplicity:** 2/5  
**Status:** New since the previous audit.

### Current evidence

The Codex worker image intentionally pins @openai/codex to 0.144.6, which is good reproducibility.

But services/worker-manager/src/codex_auth.py is now roughly 492 lines / 19.5 KB and validates detailed auth/profile structure. It grew from about 2.5 KB at the previous refresh.

services/worker-manager/src/host_profile.py is a new roughly 414-line / 14.9 KB parser layer that deliberately mirrors details of the CLI parser, including:

- duplicate-key rejection;
- nesting limits;
- lone-surrogate rejection;
- integer range handling;
- floating-point/serde_json behavior;
- JWT/session expiry extraction.

shared/contracts/dto/executor_diagnostics.py also grew substantially to carry these observations and alert state.

The implementation is careful and well tested. The debt is the coupling, not sloppy code.

### Why it matters

The worker manager is now a compatibility adapter for a private on-disk format owned by external CLIs. A CLI update can be semantically compatible for normal use but still break the diagnostic mirror.

Because admission may use these diagnostics, drift can refuse healthy executors or admit unusable ones.

### Recommendation

Keep the fail-closed diagnostics, but make the compatibility boundary explicit:

1. put each CLI format behind a named versioned adapter;
2. bind the adapter to the exact CLI version installed in the worker image;
3. maintain fixture files generated/read by that pinned CLI version;
4. make a CLI version bump fail CI until the corresponding adapter fixtures pass;
5. prefer an official CLI introspection command if/when one can provide the same facts.

Do not replace these checks with permissive parsing.

---

## M10. Core orchestration still bypasses function complexity gates

**Severity:** Medium  
**Removal safety:** 3/5  
**Removal simplicity:** 2/5  
**Status:** Newly identified in this refresh; the suppressions already existed on 2026-09-12.

### Current evidence

The root Ruff policy is active, but several important orchestration functions explicitly suppress the rules that would normally flag large decision surfaces:

- services/scheduler/src/tasks/supervisor/deploy.py::supervise_deploying_stories — C901, PLR0912, PLR0915;
- services/langgraph/src/consumers/deploy.py::process_deploy_job — C901, PLR0911, PLR0912, PLR0915;
- services/scheduler/src/tasks/server_sync.py::_sync_server_list — C901, PLR0912, PLR0915.

The surrounding modules are substantial: deploy supervision is roughly 1,613 lines and the deploy consumer roughly 834 lines.

This does not undo H4. H4 was about worker services inheriting the repository lint policy. M10 is a separate observation that some central orchestration code opts out of the most relevant complexity checks locally.

### Why it matters

These functions encode state machines, retry budgets and typed failure routes. High branch/statement counts make it easier to introduce a path that:

- skips a durable transition;
- spends a retry twice;
- loses a notification;
- conflates refusal with failure;
- becomes difficult to exercise in isolation.

### Recommendation

Refactor by state transition, not by arbitrary helper extraction.

For each function:

1. identify the closed set of typed outcomes;
2. map each outcome to a small handler with explicit inputs;
3. keep the outer function as selection/dispatch;
4. move tests to the handlers plus one routing table/coverage test;
5. remove the complexity suppression only when the state machine is actually simpler.

Take one hotspot per PR.

---

# Low severity

## L2. Legacy live-test prefix mega-test remains sweepable

**Severity:** Low  
**Removal safety:** 4/5  
**Removal simplicity:** 5/5  
**Status:** Open.

shared/live_contour.py still declares the production contour with legacy=("mega-test",).

Current code search finds the prefix only in the contour plus cleanup/guard tests, which is a good sign: it does not appear to be a current producer.

The missing step is operational proof that no resource still needs that sweep prefix.

### Recommendation

Run the production-safe inventory/sweep for that prefix, record that it is empty, then delete the legacy tuple entry and its dedicated tests.

---

## L3. Several external clients still create AsyncClient per request

**Severity:** Low  
**Removal safety:** 4/5  
**Removal simplicity:** 3/5  
**Status:** Open.

Confirmed examples on current main include:

- shared/clients/github/_base.py::_make_request();
- shared/clients/time4vps.py::_request();
- services/infra-service/src/provisioner/bitlaunch.py::get_server_ip();
- shared/clients/embedding.py::_generate_batch();
- shared/clients/registry.py::manifest_digest();
- shared/clients/infra_client.py health probe helper.

For GitHub and Time4VPS in particular, the class is long-lived conceptually but the connection pool is not.

### Recommendation

Give long-lived client objects one owned AsyncClient with explicit close/context lifecycle. Keep one-shot probes one-shot where lifecycle complexity would cost more than pooling saves.

Do not introduce a process-global HTTP singleton.

---

## L4. Written environment-default policy still contradicts actual safe defaults

**Severity:** Low  
**Removal safety:** 4/5  
**Removal simplicity:** 5/5  
**Status:** Open.

AGENTS.md still says:

- fail fast and do not add fallback values;
- environment variables — never use default values.

shared/config.py intentionally says its base fields are optional with sensible defaults and provides defaults for service name, log format, log level and default agent type.

Those defaults are not equivalent to silently defaulting a required secret or endpoint. The code is more nuanced than the rule.

### Recommendation

Narrow the convention:

- identity, credentials, connectivity and required production policy: no default;
- safe presentation/logging/local ergonomics: documented defaults allowed;
- behavior-changing production policy: explicit env/system config, not a hidden fallback.

This is a documentation fix, not a request to delete useful defaults.

---

## L5. Architecture documentation still describes retired Run accounting

**Severity:** Low  
**Removal safety:** 5/5  
**Removal simplicity:** 5/5  
**Status:** New finding.

M4 is complete: engineering token/cost accounting is canonical in engineering_attempt_ledger and the old Run compatibility fields/columns were removed.

ARCHITECTURE.md still says the runs table stores token/cost effort information. That is now misleading and contradicts the completed M4 migration.

### Recommendation

Update the monitoring section so:

- runs owns lifecycle/status/timing/result identity;
- engineering_attempt_ledger owns engineering token/cost accounting;
- Grafana reads engineering accounting from the ledger.

This can be a tiny docs-only cleanup.

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

M7 should make stale policy explicit by config class. Operational timing may reasonably use bounded last-known-good; security/admission/budget policy should not inherit that behavior accidentally.

---

# Suggested cleanup order from current main

## Phase 1 — safe, local cleanup

1. **L5:** fix ARCHITECTURE.md ledger ownership.
2. **L4:** make the env-default convention match the actual architecture.
3. **L2:** perform the legacy mega-test resource proof and delete the prefix if empty.
4. **L3:** migrate one high-frequency external client to owned connection pooling and establish the lifecycle pattern.

These are small and should not alter product state-machine semantics.

## Phase 2 — bounded compatibility/layer cleanup

1. **M2:** extract the tiny production undeploy primitive out of live_harness_cleanup and add an import-boundary test.
2. **M5:** use the supported drain/proof path, then retire the legacy temporary-access schema.
3. **M9:** formalize versioned executor-profile adapters and fixture/version gates.
4. **M7:** classify stale-safe versus fail-closed system config and bound any retained stale reads.

Each item can be delivered incrementally without a rewrite.

## Phase 3 — state-machine decomposition

1. **M10:** split one complexity-exempt orchestration function by typed outcome and remove its suppression.
2. **H1:** after ordering is durable rather than positional, separate one scheduler-pipeline responsibility from the shared tick.
3. Repeat only where tests prove the new boundary preserves at-least-once/retry/notification behavior.

H1 should be the architectural destination, but it should not be the first large refactor attempted from this audit.

---

# Overall assessment

The repository is healthier than the original audit snapshot. A large amount of transition residue has been removed, and recent work consistently moves failure handling toward typed outcomes, durable evidence and explicit recovery.

The remaining debt is concentrated rather than diffuse:

- **coordination concentration:** scheduler-pipeline still has a large ordered cycle;
- **module/layer concentration:** live-harness and runtime helpers still share an oversized ambient module;
- **compatibility residue:** temporary-access legacy rows and ConfigStore policy remain;
- **vendor-format coupling:** executor diagnostics now understand private CLI profile formats in detail;
- **complexity exemptions:** a few central state machines sit outside normal per-function complexity limits;
- **small hygiene debt:** HTTP client ownership, one legacy sweep prefix and two documentation-policy mismatches.

The next cleanup should continue the repository's existing direction: preserve typed contracts and durable evidence, then make ownership boundaries match them. The evidence does not support a rewrite.
