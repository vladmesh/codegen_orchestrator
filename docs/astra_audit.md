# Astra architecture audit

Date: 2026-09-30  
Audited branch: `main` at `cc04c5a820871a9fb3304ae7f61867c427b2ea18` (after PR #672)  
Post-merge validation: GitHub Actions CI run #2371 / run id `36697799421` completed green; Required CI Gate, scheduler/langgraph service tests, infra and backend DinD integration, template compatibility, service entrypoint imports, worker image publication and service image publication all succeeded.  
Previous full refresh: 2026-09-24 at `1b41f21e` (after PR #597).  
Scope: architecture, service/process boundaries, legacy and compatibility code, fallbacks, hidden coupling, operational complexity, and removable technical debt.

## Executive summary

The original audit is almost fully exhausted.

Of the original sixteen H/M/L findings:

- **15 are complete:** H2, H3, H4, M1-M8 and L1-L4.
- **H1 remains partially complete.** The original scheduler process split is done and five responsibilities had already been extracted into independent scheduler-pipeline loops by the 2026-09-24 refresh. PR #672 now also removes merged-PR handling and CI-failure routing from the positional dispatcher tick.
- The later findings **M9, M10 and L5 are complete** and remain complete.
- This refresh adds two small cleanup findings, **L6** and **L7**.

PR #672 materially reduces the remaining H1 blast radius. `scheduler-pipeline` now owns six independently supervised loops:

1. `task_dispatcher`;
2. `pr_ci`;
3. `worker_reconciliation`;
4. `temporary_access`;
5. `owner_notifications`;
6. `story_supervision`.

The remaining `task_dispatcher_loop` still contains one order-sensitive tick, but it no longer owns PR/CI routing, worker teardown reconciliation, temporary-access cleanup, owner-notification recovery, or story-state watchdog/stage notices.

**Current actionable set: H1 + L6 + L7.** H1 is now best treated as medium architectural debt rather than the original high-risk process concentration: an early exception can still skip later work in the same dispatcher tick, but the most important recovery and routing responsibilities now continue in sibling loops.

Open or unmerged branches are not credited here; conclusions are based on the audited `main` SHA above.

---

## What changed since the 2026-09-24 refresh

The repository continued to change heavily after the previous audit, but most work did not reopen old findings.

Architecture-relevant changes:

- PRs #598-#608 continued hardening deploy, service-image validation, live-test evidence and accounting boundaries without reintroducing the retired compatibility paths from H2-H4 or M1-M8.
- PRs #610-#626 expanded QA sandbox/evidence and live-harness behavior. Temporary-access cleanup remains an independent loop; the old dispatcher call did not return.
- PR #635 removed the RAG subsystem. This makes the old L3 embedding-client pooling concern even less relevant: that client no longer exists.
- PRs #627-#651 changed LLM-channel routing, PO behavior and capability/brief semantics without changing the scheduler finding boundary.
- PRs #652-#670 hardened secret persistence, Git credentials, empty-result settlement, deployment recovery, backup verification, bot concurrency and conflict-repair settlement. These changes strengthened durable state/fencing but did not split another dispatcher responsibility.
- **PR #672 extracted PR/CI routing from the dispatcher tick.** `poll_merged_prs` and `poll_ci_failures` now run in a sixth `scheduler-pipeline` loop, `pr_ci`, with one bounded Redis lifecycle. Merge polling and CI-failure polling also have separate exception boundaries, so failure in one does not suppress the other.
- PR #672 updated `ARCHITECTURE.md`, the service inventory tests and README to describe the six-loop topology.

No completed finding below has evidence that warrants reopening it.

---

# Current finding matrix

| ID | Original severity | Current status | Current conclusion |
|---|---|---|---|
| H1 | High | **Partial; risk reduced** | PR/CI routing joined worker reconciliation, temporary access, owner notifications and story supervision outside the dispatcher. Five responsibility groups remain in one ordered dispatcher tick. Treat as Medium now. |
| H2 | High | Complete | PO summarization tuning remains system-config owned; retired numeric env plumbing has not returned. |
| H3 | High | Complete | GitHub expected failures remain typed/status-driven rather than exception-string or empty-result fallbacks. |
| H4 | High | Complete | Worker services remain under the root Ruff policy. |
| M1 | Medium | Complete | The old worker-wrapper private task-ownership split remains removed. |
| M2 | Medium | Complete | Production deployment cleanup remains separated from the live-test harness boundary. |
| M3 | Medium | Complete | PO production startup requires durable checkpoint configuration; test/one-shot MemorySaver use is not a production downgrade path. |
| M4 | Medium | Complete | Engineering/QA token and cost accounting remains owned by `engineering_attempt_ledger`, not legacy Run fields. |
| M5 | Medium | Complete | Legacy target-less temporary-access schema/router behavior remains removed. |
| M6 | Medium | Complete | PO tools continue to use owner modules; retired compatibility re-exports have not returned. |
| M7 | Medium | Complete | ConfigStore stale reads remain explicit, bounded and allowlisted instead of global last-known-good fallback. |
| M8 | Medium | Complete | Frontend installs remain plain `npm ci`; `legacy-peer-deps` is absent. |
| M9 | Medium | Complete | Codex/Claude private credential/profile formats remain behind versioned adapters and pinned upgrade gates. |
| M10 | Medium | Complete | The three audited orchestration hotspots remain decomposed and free of the old local complexity suppressions. |
| L1 | Low | Complete | Retired live-test Makefile entrypoints remain absent. |
| L2 | Low | Complete | The `mega-test`/legacy contour path remains removed. |
| L3 | Low | Complete | Bounded HTTP lifecycles were adopted where valuable; RAG/embedding was subsequently removed entirely in PR #635. |
| L4 | Low | Complete | The narrowed environment-default rule remains documented. |
| L5 | Low | Complete | Architecture docs continue to name `engineering_attempt_ledger` as the accounting source of truth. |
| L6 | Low | **Open** | `task_dispatcher_loop` still carries dead temporary-access summary residue: `temporary_access = {}`, always-zero metrics, and an obsolete import/re-export surface. |
| L7 | Low | **Open** | `docs/NODES.md` still says the PO falls back to MemorySaver without `CHECKPOINT_DATABASE_URL`, while production startup explicitly refuses that configuration. |

K1-K4 from the original audit remain retention notes, not deletion tasks.

---

# H1. Ordered dispatcher ownership is still incomplete

**Original severity:** High  
**Current severity:** Medium  
**Status:** Partially complete, still current.

## What is already complete

The original scheduler process concentration is gone.

The current `scheduler-pipeline` service registers six sibling loops:

- `task_dispatcher`;
- `pr_ci` — added by PR #672;
- `worker_reconciliation` — terminal/gave-up worker teardown reconciliation;
- `temporary_access`;
- `owner_notifications`;
- `story_supervision` — state-age watchdogs and stage notices.

Each loop is run through the shared scheduler runtime as a separate long-lived worker. A cycle exception in one loop does not stop the other loops.

PR #672 additionally gives the two operations inside `pr_ci` separate failure boundaries: a failure in merged-PR polling does not prevent CI-failure routing in that cycle, and vice versa.

## What remains in `task_dispatcher_loop`

On audited main, one dispatcher tick still executes these groups in this order:

1. **Scaffold triggering** — `trigger_scaffolds`;
2. **Engineering dispatch** — `dispatch_todo_tasks`;
3. **Story completion / PR creation** — `complete_stories`;
4. **Lifecycle supervision** — stuck stories, stuck tasks, failed tasks, waiting resources, deploying stories and waiting-user-secret stories;
5. **QA/testing routing** — `supervise_testing_stories`.

Those calls still sit under one outer `try/except`. An uncaught failure in an earlier group skips the later groups until the next dispatcher cadence.

That is now a bounded scheduling-delay problem, not the original broad recovery failure: PR/CI handling, worker reconciliation, temporary-access recovery, owner-notification recovery and story-state supervision continue independently.

## Recommended next H1 slice: scaffold triggering

The next low-risk extraction is `trigger_scaffolds`.

The ordering dependency is already represented durably at the admission point:

- a DRAFT project is refused with `project_not_scaffolded`;
- an ACTIVE project without a ready workspace is refused with `workspace_not_ready`;
- ensure-workspace failure records `scaffold_error` and routes through the existing infrastructure-park recovery path.

Therefore dispatch does not need scaffold triggering to have run earlier in the same process tick. It only needs the durable project/workspace facts to become true eventually.

### Acceptance boundary for that slice

A future scaffold-loop PR should prove all of the following:

1. `scheduler-pipeline` registers a named independent scaffold-trigger loop using the existing dispatch cadence unless a separate cadence is justified.
2. The loop owns its own Redis connection lifecycle and per-cycle exception boundary.
3. `task_dispatcher_loop` no longer calls `trigger_scaffolds`.
4. A scaffold-loop failure cannot stop dispatcher ticks, and a dispatcher failure cannot stop scaffold triggering.
5. Dispatch still fails closed through the existing `project_not_scaffolded` / `workspace_not_ready` admission decisions; no new in-memory coordination or compatibility flag is introduced.
6. Existing ensure-workspace failure/recovery tests remain green.
7. Architecture/service inventory docs are updated in the same PR.

L6 is in the same dispatcher area and can be removed while that file is already being touched, provided the diff stays mechanical.

## After the scaffold slice

Re-audit the remaining four groups before extracting another one. Do not mechanically split them just to reach “one loop per function”.

The likely candidates are:

- **story completion**: potentially independent because it selects durable story/task state and writes PR-review state;
- **QA/testing routing**: potentially independent, but must preserve its durable relationship with deployment/temporary-access facts;
- **lifecycle supervisors**: highest coupling of the remaining groups and should be split only around explicit durable ownership boundaries;
- **engineering dispatch**: likely remains the natural core dispatcher responsibility.

The goal is not maximum process count. The goal is that no responsibility depends on call position when the dependency can be represented as durable state.

---

# L6. Remove dead temporary-access residue from task_dispatcher

**Severity:** Low  
**Status:** Open  
**Safety:** High.

Temporary-access supervision moved to its own loop in PR #573, but the dispatcher still contains residue from the transition:

- `temporary_access = {}`;
- always-zero temporary-access terms in `supervisor_active`;
- always-zero temporary-access fields in `supervisor_cycle`;
- the obsolete `supervise_temporary_access` import/re-export used only by old dispatcher test plumbing.

This code no longer observes real temporary-access work and can mislead operators reading the dispatcher log schema.

**Fix:** remove the dead variable, zero-only log fields, obsolete import/re-export and tests that exist only to prove the dispatcher does not call the sweep. The independent `temporary_access_loop` tests remain the authority.

This is a mechanical cleanup, not a behavior change.

---

# L7. Correct PO checkpoint fallback documentation

**Severity:** Low  
**Status:** Open  
**Safety:** Very high; documentation only.

`docs/NODES.md` currently says that without `CHECKPOINT_DATABASE_URL` the PO uses in-memory `MemorySaver`.

Current production wiring says otherwise:

- `services/langgraph/src/main.py` raises when the PO consumer is enabled without `CHECKPOINT_DATABASE_URL`;
- the setting is documented as required when PO is enabled;
- `MemorySaver` remains valid for tests and one-shot/non-PO graph uses.

**Fix:** make `docs/NODES.md` describe PostgreSQL as required for the production PO consumer and explicitly scope MemorySaver to tests/explicit graph construction.

No code change is needed.

---

# Completed findings: retention checks

These findings should not be reopened without new concrete evidence.

## H2 — PO summarization configuration

Complete. Runtime tuning remains under the intended system-config boundary rather than duplicated numeric environment variables.

## H3 — GitHub error classification

Complete. Expected GitHub states continue to use typed/status-driven handling. No return to exception-string classification was found.

## H4 — worker lint policy

Complete. Worker code remains inside the repository Ruff policy.

## M1 — worker-wrapper task ownership

Complete. The obsolete private `self._task` ownership split remains gone.

## M2 — production/live-harness dependency

Complete. Production deployment cleanup stays in the production/shared boundary; remaining `shared.live_harness_cleanup` imports are live-test/harness code, not a production service dependency.

## M3 — PO checkpoint durability

Complete. Production PO startup refuses missing checkpoint DB configuration. The open issue is only the stale sentence in `docs/NODES.md` (L7), not a runtime fallback regression.

## M4 / L5 — accounting source of truth

Complete. `engineering_attempt_ledger` remains append-only accounting authority and architecture/contract docs name it explicitly.

## M5 — temporary-access legacy schema

Complete. Target-backed grants are canonical; the legacy slot-era router/schema path remains retired.

## M6 — PO compatibility exports

Complete. No reason to restore the removed compatibility re-export layer.

## M7 — ConfigStore fallback

Complete. Stale values remain opt-in, bounded and key-classified.

## M8 — frontend dependency installation

Complete. `legacy-peer-deps` remains absent.

## M9 — coding-agent private format adapters

Complete. Vendor-private Codex/Claude profile parsing remains version-bound rather than leaked through generic host-profile logic.

## M10 — orchestration complexity hotspots

Complete. The audited server-sync, deploy-consumer and deploying-story supervisor decompositions remain in place.

## L1/L2 — retired live-test surfaces

Complete. Retired entrypoints and the `mega-test` legacy contour are absent.

## L3 — HTTP client lifecycle

Complete. The bounded lifecycle work remains valid, and PR #635 removed the RAG/embedding subsystem that previously supplied one of the audited clients.

## L4 — environment-default rule

Complete. No evidence from this refresh justifies reopening it.

---

# Recommended cleanup order

1. **Next functional PR — H1 scaffold-loop extraction + L6 mechanical residue cleanup.**
   Keep the behavior change limited to moving scaffold triggering onto its own durable cadence/failure boundary; remove the dead temporary-access dispatcher fields while the same module/tests are already touched.
2. **Tiny documentation correction — L7.**
   This can be included with the next documentation-touching PR or closed separately; it should not block functional work.
3. **Re-audit the reduced dispatcher after the scaffold extraction.**
   Decide from concrete call-order dependencies whether story completion or QA/testing is the next safe independent loop.
4. **Leave lifecycle supervisors until their cross-state ordering is proven unnecessary.**
   Do not split them based only on function size.
5. **Do not revisit H2-H4, M1-M10 or L1-L5 without new evidence.**

The audit is now intentionally small: it is no longer a general cleanup backlog. It tracks one remaining architectural seam and two low-risk residue/documentation items.

---

# Validation notes for this refresh

The refresh was based on:

- current `main` source after PR #672;
- current scheduler-pipeline worker inventory;
- the actual remaining `task_dispatcher_loop` call sequence;
- current engineering-dispatch refusal contracts for scaffold/workspace readiness;
- regression searches for previously retired paths such as `legacy-peer-deps`, `Contour.legacy`, `mega-test`, legacy Run accounting and the production checkpoint fallback;
- merged PR history since the previous 2026-09-24 audit;
- PR #672's green pre-merge Required CI Gate;
- post-merge main CI run #2371, including green Required CI Gate and successful image publication.

This PR remains a living audit branch and should **not** be merged.
