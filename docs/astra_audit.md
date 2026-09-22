# Astra architecture audit

Date: 2026-09-22  
Audited branch: main at a741e2901e20c0395db701428f948aba6b781fff (after PR #559)  
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

- **11 remain complete:** H2, H3, H4, M1, M2, M3, M4, M6, M7, M8 and L1.
- **H1 remains partially complete.**
- **4 remain open:** M5 and L2-L4.

This refresh adds three findings:

- **M9 — partially remediated via PR #559:** Codex's private 0.144.6 profile format now has a named versioned adapter and a worker-image version gate; Claude is still installed from a floating installer and its private credential format is not version-bound.
- **M10 — complete:** PR #554 removed the server-sync exemption, PR #555 decomposed the LangGraph deploy consumer, and PR #557 decomposed scheduler `supervise_deploying_stories`; all three audited orchestration hotspots are back under the normal complexity gates.
- **L5 — newly identified documentation drift:** ARCHITECTURE.md still says Run rows hold engineering token/cost accounting even though M4 made engineering_attempt_ledger canonical.

So the current actionable set is one High finding, two Medium findings and four Low findings. M9 is now partial rather than wholly open: the Codex upgrade boundary is explicit, while the Claude side and remaining parser-coupling/provenance work remain. The completed work should stay completed; no rewrite is justified.

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

These changes mostly harden correctness. H1 remains because scheduler-pipeline still owns one ordered multi-responsibility cycle, but M10 is now complete: the server-sync, LangGraph deploy-consumer and scheduler deploy-supervisor hotspots all use bounded routing phases without local complexity suppressions. PR #553 closed the M2 production-to-harness dependency, PR #558 closed M7 by making stale config use explicit, bounded and key-classified, and PR #559 made the Codex half of M9 an explicit versioned compatibility boundary.

---

# Current finding matrix

| ID | Severity | Status on 2026-09-22 | Current conclusion |
|---|---|---|---|
| H1 | High | Partial | Three scheduler services exist, but scheduler-pipeline still has one ordered dispatcher/supervisor/reconciliation cycle and one cycle failure boundary. |
| H2 | High | Complete | System config is canonical for PO summarization tuning; retired numeric env plumbing remains absent. |
| H3 | High | Complete | GitHub expected failures remain status-driven rather than exception-string/empty-result fallbacks. |
| H4 | High | Complete | Worker services remain under the root Ruff policy. |
| M1 | Medium | Complete | The old worker-wrapper private task ownership split is gone. |
| M2 | Medium | Complete | PR #553 moved the undeploy/recovery primitive to shared.deployment_cleanup and added a guard forbidding production service imports from shared.live_harness*. |
| M3 | Medium | Complete | Production PO startup requires durable checkpoint configuration; MemorySaver remains an explicit graph/test-capable path rather than the enabled production downgrade. |
| M4 | Medium | Complete | Run accounting compatibility is gone and the engineering attempt ledger is canonical. |
| M5 | Medium | Open; safer remediation | Legacy temporary-access columns/branches remain, but operator drain now provides a supported path to eliminate unreconcilable live legacy rows. |
| M6 | Medium | Complete | PO tools use owner modules; retired compatibility re-exports remain removed. |
| M7 | Medium | Complete | PR #558 made stale reads opt-in and bounded; unclassified config fails closed and only a small scheduler cadence allowlist may use a 15-minute last-known value. |
| M8 | Medium | Complete | Frontend images use plain npm ci; legacy-peer-deps is absent. |
| M9 | Medium | Partial via PR #559 | Codex 0.144.6 now has a named format adapter and a CLI-pin mismatch gate; Claude remains floating/unversioned and some Codex-specific serde_json emulation still lives in the shared parser layer. |
| M10 | Medium | Complete | PRs #554, #555 and #557 decomposed all three audited orchestration hotspots; server-sync, LangGraph deploy consumption and scheduler deploy supervision no longer carry the relevant complexity suppressions. |
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
**Status:** Partially complete via PR #559; Codex is version-bound, Claude is not.

### What PR #559 completed

The Codex worker image still pins `@openai/codex` to `0.144.6`. PR #559 now makes the matching parser contract explicit:

- `services/worker-manager/src/codex_profile_v01446.py` is the named ~231-line compatibility adapter for Codex 0.144.6. It owns the private `AuthDotJson` / `TokenData` / `AuthMode` shape, JWT claim compatibility and the pinned format refusals.
- `services/worker-manager/src/codex_auth.py` shrank from roughly 492 lines to roughly 288 lines. It keeps the non-format responsibilities: stable file reads, advisory locking, file permissions, config checks, metadata interpretation and health classification.
- a regression test reads `services/worker-manager/images/worker-base-codex/Dockerfile` and requires its `CODEX_CLI_VERSION` to equal the adapter's `CODEX_CLI_VERSION`. A future Codex version bump therefore fails CI until the adapter is deliberately updated/replaced.
- the existing admission/diagnostic compatibility matrix still exercises malformed/accepted fields, auth-mode precedence, JWT claims, serde_json edge cases, lock races and credential-safe refusal behavior through the same public reader.

The final PR #559 gate passed Ruff formatting/lint, the full unit suite, shared freshness, offline live regressions, Redis cleanup, worker-manager service tests, applicable integration/template checks and production service-image entrypoint imports. It merged as `a741e2901e20c0395db701428f948aba6b781fff`.

This materially reduces Codex upgrade blast radius: the private vendor format is a named replaceable boundary, and the image version cannot silently drift away from that boundary.

### What remains

M9 is not complete yet.

The Claude worker image does **not** currently pin an exact Claude Code version. Its installer stage runs `curl -fsSL https://claude.ai/install.sh | bash`, so a rebuild can pick up a newer CLI without a repository change.

`services/worker-manager/src/claude_auth.py` is only about 110 lines, but it still directly understands Claude's private `.credentials.json` / `claudeAiOauth` fields such as `accessToken`, `refreshToken` and `expiresAt`. There is no named versioned Claude adapter and no image-version-to-adapter gate comparable to Codex.

The shared `services/worker-manager/src/host_profile.py` also still contains Codex-specific serde_json 1.0.149 emulation (duplicate-key, surrogate, numeric-range and nesting semantics). PR #559 moved the high-level Codex file-format mirror out of the generic reader, but this lower-level vendor parser behavior remains shared infrastructure.

Finally, the tests are strong synthetic compatibility tests, but the audit recommendation to keep fixtures with explicit provenance from the exact pinned CLI version is not yet fully realized.

### Remaining recommendation

Finish M9 incrementally:

1. pin the Claude Code worker image to an exact reproducible CLI release/artifact;
2. put Claude's private credential format behind a named adapter bound to that exact version;
3. keep the generic stable-read / credential-safe observation layer vendor-neutral, moving or encapsulating the remaining Codex-only serde_json emulation behind the Codex adapter boundary;
4. add version-provenance fixtures or an equivalent compatibility contract for each pinned CLI;
5. prefer an official CLI introspection command if either vendor exposes one that provides the same fail-closed facts.

Do not replace these checks with permissive parsing.

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

1. **L5:** fix ARCHITECTURE.md ledger ownership.
2. **L4:** make the env-default convention match the actual architecture.
3. **L2:** perform the legacy mega-test resource proof and delete the prefix if empty.
4. **L3:** migrate one high-frequency external client to owned connection pooling and establish the lifecycle pattern.

These are small and should not alter product state-machine semantics.

## Phase 2 — bounded compatibility/layer cleanup

1. **M5:** use the supported drain/proof path, then retire the legacy temporary-access schema.
2. **M9:** finish the compatibility boundary after PR #559: pin Claude, add its versioned adapter/gate, then move the remaining Codex-only serde_json emulation/provenance behind the vendor boundary.

Each item can be delivered incrementally without a rewrite.

## Phase 3 — scheduler boundary decomposition

1. **H1:** after ordering is durable rather than positional, separate one scheduler-pipeline responsibility from the shared tick.
2. Repeat only where tests prove the new boundary preserves at-least-once/retry/notification behavior.

M10 is complete. H1 is now the remaining architectural destination, and it should still be approached one responsibility at a time rather than as a scheduler rewrite.

---

# Overall assessment

The repository is healthier than the original audit snapshot. A large amount of transition residue has been removed, and recent work consistently moves failure handling toward typed outcomes, durable evidence and explicit recovery.

The remaining debt is concentrated rather than diffuse:

- **coordination concentration:** scheduler-pipeline still has a large ordered cycle;
- **harness concentration:** live-harness remains oversized, but PR #553 removed the production import dependency and pinned that boundary;
- **compatibility residue:** temporary-access legacy rows remain;
- **vendor-format coupling:** Codex now has a versioned 0.144.6 adapter/gate, but Claude remains floating and unversioned and lower-level Codex serde_json compatibility still leaks into the shared parser layer;
- **small hygiene debt:** HTTP client ownership, one legacy sweep prefix and two documentation-policy mismatches.

The next cleanup should continue the repository's existing direction: preserve typed contracts and durable evidence, then make ownership boundaries match them. The evidence does not support a rewrite.
