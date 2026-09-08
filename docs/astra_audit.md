# Astra architecture audit

Date: 2026-09-06  
Audited branch: `main` (starting around `50d9753ba0b5b94d5edbe43314330d1b52aaddb4`)  
Scope: architecture, service boundaries, legacy/compatibility code, fallbacks, shims, hidden coupling, operational complexity, and removable technical debt.

## Executive summary

The repository is in better shape than the words `legacy`, `fallback`, and `compatibility` initially suggest. Several important boundaries are deliberate and strong:

- service-to-service calls are centralized through the internal API client;
- Redis stream contracts are typed and shared;
- worker execution is isolated behind `worker-manager` / `worker-broker`;
- paid-work admission is centralized rather than reimplemented by consumers;
- QA capability isolation is unusually explicit and defensive;
- queue recovery semantics, idempotency, and terminal outcomes are documented instead of being implicit.

The main architectural problem is therefore **not** that the system has too many services. The bigger issue is that a few old transition mechanisms and one oversized orchestration process cut across otherwise good boundaries.

The highest-value cleanup areas are:

1. split the scheduler's giant process / giant dispatcher cycle into independently owned workers;
2. remove silent config fallback paths and choose one source of truth per setting;
3. make GitHub client failures typed instead of guessing from exception strings or converting failures into empty data;
4. clean the `worker-manager` quality island that is currently hidden behind a Ruff version pin;
5. remove lifecycle ownership hacks in `worker-wrapper`;
6. progressively delete explicit compatibility surfaces once their data/producers are proven gone.

No finding in this pass looks like a **Critical** architectural defect. The highest findings are **High** because they increase blast radius, hide failure, or preserve a substantial island of old behavior.

## Rating scale

Each cleanup item has three ratings:

- **Severity**: `High`, `Medium`, `Low` — impact of leaving the debt in place.
- **Removal safety**: `1/5` means deleting/refactoring it is risky without a migration; `5/5` means it is very safe once tests pass.
- **Removal simplicity**: `1/5` means a substantial architectural migration; `5/5` means a small localized cleanup.

"Removal" means removing the debt, shim, fallback, duplicated responsibility, or compatibility path — not deleting the product feature it currently supports.

## Refresh note — 2026-09-08

This is a lightweight status refresh against the current repository after several cleanup PRs, **not** a new deep audit. The original evidence below is intentionally kept as the historical reason each item was raised; use these notes before taking the next item so stale evidence is not mistaken for current `main`.

### Completed or materially reduced

- **H2 — core issue completed in PR #476 (merged 2026-09-08).** Production PO numeric summarization tuning now comes from required `llm.summarization_*` system config, ConfigStore failures no longer silently switch policy to Settings defaults, startup fails before unrelated langgraph loops, and logs report effective values. A small cleanup residue remains: old numeric `SUMMARIZATION_*` env documentation / compose / workflow wiring is still present even though it is no longer the production source of truth. Treat that as dead configuration plumbing to recheck separately, not as the original H2 runtime bug.
- **H3 — completed in PR #477 (merged 2026-09-08).** GitHub repository/file paths no longer classify errors by matching `"422"` / `"already exists"` in exception text or turn arbitrary failures into empty/missing resources. Expected 404/422 cases are status-driven, 422 repository creation is verified with `get_repo()`, and transport/auth/server/unexpected failures retain failure semantics. The narrow repo-token → org-token deletion fallback was deliberately preserved.
- **M1 — immediate ownership hack completed in PR #463 (merged 2026-09-07).** The private `_task` / `stop_event` split ownership described below is gone. Revisit only if a richer graceful-stop API is actually needed.
- **M3 — completed in PR #464 (merged 2026-09-07).** Enabled PO now requires durable checkpoint configuration at startup; `MemorySaver` remains an explicit test construction path rather than a production downgrade.

### Recheck / refresh before implementation

- **H1:** quick check says the scheduler still has the same broad multi-domain ownership shape. The item remains current, but this area has changed heavily since 2026-09-06; reread current `scheduler.main`, startup keys, and dispatcher responsibilities before designing the split rather than implementing the exact process list below mechanically.
- **H4:** **original evidence is materially stale.** PR #475 moved the root dependency to `ruff>=0.16,<0.17`, so there is no longer a root `<0.16` pin hiding the subtree. The remaining issue is that `worker-manager` and `worker-broker` intentionally keep local Ruff 0.16 defaults and 120-character formatting instead of the full repository rule set. Reframe H4 as a **local quality-policy island**, and re-count the current violations against the root rule set before taking it; do not use the old “132 violations / remove the version pin” plan as-is.
- **M2:** quick check still finds production `deploy_lifecycle.py` importing `shared.live_harness_cleanup`; the boundary smell remains. Recheck the wider `shared/` dependency graph before a large move because recent template/live-harness work may have changed what is genuinely runtime-owned.
- **M4:** `_attach_ledger_compatibility()` still exists. The item remains current, but consumer/data-migration proof should be refreshed before dropping fields or columns.
- **M5:** legacy temporary-access columns and `_LEGACY_REMEDIATION` guards still exist. This remains a proof/data-state task; recheck live DB invariants before changing it.
- **M6:** the PO `tools.py` backward-compatibility re-export facade and old patch targets still exist. Still current and still likely localized; rerun code search before deletion.
- **M7 / K4:** ConfigStore last-known-good behavior still exists. H2 makes the policy distinction more important, not less: startup-critical required config can fail fast while some already-running operational reads may still tolerate last-known-good. Classify keys/callers before changing ConfigStore globally.
- **M8:** both frontend Dockerfiles still use `npm ci --legacy-peer-deps`; item remains current.
- **L1 / L2:** the Makefile aliases/legacy aggregate and `mega-test` cleanup prefix still exist. Both remain low-risk cleanup candidates, but L2 still needs the operational resource sweep/proof described below.
- **L3, L4, K1, K2:** not revalidated in detail in this refresh. Treat their evidence as “recheck before taking”, not as confirmed stale or confirmed current.
- **K3:** explicitly revalidated while doing H3 and intentionally kept; its narrow 404-driven repository-deletion fallback is still a legitimate exception to the broad no-fallback rule.

The **Suggested cleanup order** at the end is therefore partly historical: Phase 1 items for GitHub handling, worker-wrapper ownership, PO checkpoint durability, and PO summarization runtime fallback are already done. Use the refresh notes above when selecting the next iteration.

---

# High severity

## H1. `scheduler` is a multi-domain mega-process

**Severity:** High  
**Removal safety:** 3/5  
**Removal simplicity:** 1/5

### Evidence

`services/scheduler/src/main.py` starts all of these in one process:

- server sync;
- GitHub sync;
- health checker;
- RAG summarizer;
- provisioner result listener;
- task dispatcher;
- analytics aggregator;
- queue cleanup.

At the same time, `services/scheduler/src/startup.py` validates a large shared set of keys spanning scheduler, deploy, supervisor, health, and temporary-access policy.

The dispatcher itself (`services/scheduler/src/tasks/task_dispatcher.py`) is also an orchestration bundle. One periodic tick performs scaffold triggering, engineering admission and publication, story completion, PR polling, CI failure routing, stuck story/task supervision, resource waiting, deploy supervision, user-secret supervision, owner-notification recovery, QA routing, and temporary-access cleanup.

The comments in that function correctly explain subtle ordering constraints, but those comments are also evidence that many independently meaningful workflows share one clock and one failure boundary.

### Why it matters

This creates a large blast radius:

- one process crash restarts unrelated background jobs;
- one worker cannot be scaled independently;
- startup config for one concern can prevent unrelated concerns from starting;
- the dispatcher cycle accumulates ordering dependencies that are difficult to test compositionally;
- latency of one concern can stretch the cycle time of all later concerns;
- ownership becomes "scheduler owns everything periodic" instead of following bounded contexts.

The code already has good module boundaries, so the process boundary is lagging behind the code boundary.

### Recommendation

Keep the same image initially, but run separate entrypoints/processes for at least:

1. `task-dispatcher` + story completion / PR lifecycle;
2. `pipeline-supervisor` (stuck/retry/resource/deploy/QA state routing);
3. `temporary-access-supervisor` + owed owner notifications if ordering still requires them together;
4. `infrastructure-observers` (server sync / health / provisioning);
5. `analytics-maintenance` (analytics, RAG summarizer, queue cleanup).

Do **not** begin by creating five new repositories or deployment stacks. First separate runtime ownership while reusing the same package and image.

### Safe removal path

1. Give each loop its own entrypoint.
2. Move required config validation next to the process that consumes it.
3. Preserve current tick ordering inside the pipeline-supervisor process.
4. Add integration tests for cross-process state transitions.
5. Only then delete the aggregate `scheduler.main` runner.

---

## H2. PO summarization configuration has multiple sources plus a silent broad fallback

**Severity:** High  
**Removal safety:** 4/5  
**Removal simplicity:** 4/5

### Evidence

`services/langgraph/src/config/settings.py` contains environment-backed summarization defaults:

- `summarization_max_tokens = 20000`
- `summarization_trigger_tokens = 70000`
- `summarization_max_summary_tokens = 2000`

The same values also exist in `.env.example` / compose wiring, while `scripts/system_configs.yaml` defines DB-backed `llm.summarization_*` values (including a different `summarization_max_tokens` value of `50000`).

Then `services/langgraph/src/consumers/po.py` does this:

1. create a `ConfigStore`;
2. read DB values with Settings values as defaults;
3. catch **any** exception;
4. silently use Settings values instead.

The resulting log also reports `settings.summarization_*`, not necessarily the effective DB-backed values that were actually passed to the graph.

### Why it matters

This violates the repository's own stated "fail fast / no speculative fallback" rule and makes production behavior dependent on which configuration source happened to answer.

A typo, API bug, malformed response, auth failure, or programming error in ConfigStore can silently turn into a different summarization policy instead of a visible startup failure.

It also makes it difficult to answer a basic operational question: "what value is canonical?"

### Recommendation

Choose one source of truth.

For production, the cleanest current model is:

- system config DB is canonical for operational tuning;
- environment contains only bootstrapping information required to reach the config service;
- tests may inject explicit values directly.

Then remove the broad `except Exception` fallback and make missing/invalid required config fail startup.

If local standalone execution truly needs env-only configuration, make it an explicit mode such as `CONFIG_SOURCE=env`, not an automatic fallback.

### Safe removal path

This is relatively safe because compose already starts PO with API connectivity and the repo already has `ConfigStore` startup patterns in scheduler. Add one startup validation test, report the effective values in logs, then delete the fallback chain.

---

## H3. GitHub client contains failure-hiding fallbacks that can change semantics

**Severity:** High  
**Removal safety:** 5/5  
**Removal simplicity:** 4/5

### Evidence

Several patterns in `shared/clients/github/` conflict with the otherwise contracts-first/fail-fast architecture.

### A. Exception-string parsing

`shared/clients/github/_provisioning.py` handles repo creation idempotency correctly for `httpx.HTTPStatusError`, but then has a second broad branch:

```python
except Exception as e:
    if "422" in str(e):
        ... use existing repo ...
```

That converts unrelated exceptions whose text happens to contain `422` into "repository already exists".

### B. Failure becomes empty directory

`shared/clients/github/_repos.py::list_repo_files()` returns `[]` on any broad exception after logging a warning.

An empty repository and a failed GitHub call are therefore observationally identical to callers.

### C. File existence probe suppresses arbitrary failures

`create_or_update_file()` catches broad exceptions while checking the existing file SHA and proceeds as though creation may be appropriate.

A transient auth/network/programming failure in the read phase can therefore change an intended update into a create attempt and defer the real error to a later API call.

### Why it matters

These are exactly the kinds of fallback branches the repository's own `AGENTS.md` says to avoid: they hide missing facts and make downstream behavior depend on guessed meaning.

### Recommendation

- Delete exception-string parsing entirely.
- Catch only concrete `httpx` / GitHub error classes and status codes.
- Make "not found" a typed normal result where needed.
- Propagate transport/auth/server failures.
- Where callers legitimately want best-effort behavior, put that policy at the caller and name it explicitly (`best_effort_list_repo_files`) rather than weakening the shared primitive.

### Note on the org-token fallback

The repo-scoped-token → org-token fallback in `delete_repo()` is different: it is narrow, status-code driven, documented, and corresponds to a real GitHub App installation race for newly created repos. I would **not** remove it just because it contains the word "fallback".

---

## H4. `worker-manager` is an intentionally hidden old-code island

**Severity:** High  
**Removal safety:** 5/5  
**Removal simplicity:** 2/5

### Evidence

The root `pyproject.toml` pins Ruff below `0.16` and explains why:

> 0.16 ... stops treating services/worker-manager as its own config root, which surfaces 132 real violations there (blind except, typing.Dict, unsorted imports, naive datetimes).

`services/worker-manager/pyproject.toml` carries its own minimal Ruff config with a different line length, while the repository root has the actual lint policy.

This is a very useful comment because it clearly identifies deferred debt, but it also means the current toolchain is intentionally configured not to see a substantial set of real problems in one security-sensitive service.

### Why it matters

`worker-manager` owns Docker daemon access, worker creation, host paths, network placement, and agent credentials. It is one of the worst places to preserve a lint/typing/exception-handling blind spot.

The issue is not the Ruff version itself. The issue is that repository quality gates are not uniform across the control-plane service with the highest host privilege.

### Recommendation

Make "Ruff 0.16 cleanup in worker-manager" a bounded migration:

1. run the newer rule set against that subtree;
2. mechanically fix imports / typing / naive datetime first;
3. manually review blind `except` and behavior-changing warnings;
4. remove the service-local Ruff config if it is no longer needed;
5. remove the root `<0.16` pin.

Do not combine this with product behavior work. The payoff is mostly removing a blind spot from future reviews.

---

# Medium severity

## M1. `worker-wrapper` lifecycle ownership is split between `main` and `WorkerWrapper`

**Severity:** Medium  
**Removal safety:** 5/5  
**Removal simplicity:** 4/5

### Evidence

`packages/worker-wrapper/src/worker_wrapper/main.py` contains a literal `# Hack for now` block.

The entrypoint:

- creates an unused `stop_event`;
- writes to private `wrapper._task`;
- cancels that private task from the signal handler;
- contains comments discussing a missing `stop()` method rather than a settled lifecycle contract.

Meanwhile `WorkerWrapper` already owns `_running`, broker shutdown, and the run loop.

### Why it matters

There are two owners of shutdown state. This makes graceful termination harder to reason about and encourages future callers to depend on private fields.

### Recommendation

Give `WorkerWrapper` a public async lifecycle:

- `start()` / `run()`;
- `stop()` sets `_running = False` and cancels/interrupts the current wait if necessary;
- `close()` if resource cleanup needs a separate phase.

The signal handler should call the public stop path, not know about `_task`.

`stop_event` and the hack comments can then disappear.

### Progress

**First iteration completed in PR #463 (merged 2026-09-07).** The concrete ownership hack was removed without broadening shutdown semantics:

- `worker_wrapper.main` now owns a local task for `WorkerWrapper.run()`;
- SIGTERM/SIGINT cancel that entrypoint-owned task directly;
- the unused `stop_event`, private `WorkerWrapper._task` state, and temporary hack commentary were removed;
- existing broker cleanup and agent shutdown behavior were intentionally left unchanged.

This resolves the immediate split task-ownership problem. A public `stop()` / `close()` lifecycle remains a possible follow-up only if graceful-stop semantics beyond task cancellation are needed; it was not required for this first cleanup.

---

## M2. `shared/` mixes contracts/runtime libraries with live-test harness machinery

**Severity:** Medium  
**Removal safety:** 2/5  
**Removal simplicity:** 1/5

### Evidence

`shared/` is copied or bind-mounted wholesale into many services rather than being a versioned/installable package.

It contains clean cross-service runtime modules (`contracts`, Redis, internal API clients), but also live harness modules such as:

- `shared/live_contour.py`
- `shared/live_harness_cleanup.py`

`shared/live_harness_cleanup.py` is ~27 KB and is explicitly listed as a deferred offender in `shared/tests/unit/test_internal_api_transport.py`.

More importantly, production `services/langgraph/src/consumers/deploy_lifecycle.py` imports cleanup helpers from a module named `live_harness_cleanup`. That is a boundary smell in both directions: test/harness code is in the runtime shared package, and runtime deploy code consumes it.

### Why it matters

`shared` is effectively a monorepo-wide ambient dependency. Any addition there becomes available everywhere and is copied into multiple images. That weakens service dependency ownership and makes test-only/runtime-only separation difficult to enforce.

### Recommendation

Split by responsibility before considering packaging:

- `shared/contracts/` — DTOs/enums/queue schema only;
- `shared/runtime/` or equivalent — genuinely shared clients and primitives;
- `tests/live/harness/` — stand/live test mechanics;
- move any genuinely reusable remote cleanup primitive out of the test-named module into a runtime lifecycle module, then have the harness import it rather than the reverse.

Only after the dependency graph is clean is it worth deciding whether `shared` should become one or several installable packages.

---

## M3. PO checkpointer silently falls back from durable PostgreSQL to `MemorySaver`

**Severity:** Medium  
**Removal safety:** 5/5  
**Removal simplicity:** 5/5

### Evidence

`services/langgraph/src/config/settings.py` makes `CHECKPOINT_DATABASE_URL` optional and `services/langgraph/src/agents/po/graph.py` logs a warning then creates `MemorySaver()` when it is absent.

Production compose always supplies a PostgreSQL checkpointer URL.

### Why it matters

A misconfigured PO process can start successfully while silently losing durable conversation state across restart. For a central user-facing coordinator, that is a meaningful semantic downgrade, not a harmless local default.

This also contradicts the general fail-fast project convention.

### Recommendation

Make persistence required at the PO consumer boundary.

If unit tests need memory persistence, pass `MemorySaver` or `checkpoint_database_url=None` explicitly from test construction rather than making production startup accept the downgrade.

### Progress

**Implemented in PR #464 (open as of 2026-09-07).** The cleanup is intentionally scoped to the production PO startup boundary:

- when PO LLM configuration enables the PO consumer, `CHECKPOINT_DATABASE_URL` is now required before the langgraph background loops start;
- missing durable persistence raises immediately instead of allowing PO to start with in-memory conversation state;
- `checkpoint_database_url` remains optional in the shared langgraph `Settings`, because the same process owns non-PO consumers that do not require this checkpointer;
- explicit `create_po_graph(..., checkpoint_database_url=None)` / `MemorySaver` construction remains available for unit tests;
- a startup unit test pins the fail-fast behavior.

Production compose already supplies the PostgreSQL URL, so the normal production path is unchanged; the removed behavior is only the silent durability downgrade under misconfiguration. This item is complete once PR #464 merges.

---

## M4. Run observability still projects canonical ledger data back into compatibility fields

**Severity:** Medium  
**Removal safety:** 3/5  
**Removal simplicity:** 3/5

### Evidence

`services/api/src/routers/runs.py::_attach_ledger_compatibility()` explicitly says:

> Expose old Run observability fields as projections of the ledger.

It reads `EngineeringAttemptLedger`, then assigns transient `_ledger_*` attributes so old Run response fields continue to appear populated. The comment also says the old DB columns remain only for rolling compatibility and new engineering writes do not populate them.

### Why it matters

This is a classic dual-model compatibility surface: the canonical model is already the ledger, but API shape and old columns keep the old representation alive.

Every new use of the old Run fields extends the migration indefinitely.

### Recommendation

Treat this as a deletion project with an explicit deadline:

1. search API/frontend/tests for old Run token/cost fields;
2. migrate consumers to a named ledger/attempt view;
3. add a contract test forbidding new uses of the old fields;
4. remove `_attach_ledger_compatibility`;
5. drop old DB columns in Alembic.

Do not remove the projection before verifying admin/dashboard consumers and historical response expectations.

---

## M5. Temporary-access lifecycle still carries a retired schema and runtime guards

**Severity:** Medium  
**Removal safety:** 2/5  
**Removal simplicity:** 3/5

### Evidence

`shared/models/temporary_access_grant.py` retains:

- `legacy_env_key` mapped to old `env_key`;
- `legacy_subject` mapped to old `subject`.

The comment says new records never populate them and they exist only so terminal slot history is readable.

`services/api/src/routers/temporary_access.py` additionally contains:

- `_LEGACY_REMEDIATION`;
- `_is_legacy()`;
- `_reject_legacy_record()`;
- `_reject_live_legacy()`;
- special list behavior that includes legacy history only for a QA-run lookup.

This is good defensive migration code, but it is still a nontrivial amount of runtime branching for a retired lifecycle.

### Why it matters

Unlike a harmless historical column, the old model still affects create/list/get/update semantics and can block new capability-backed grants if a live legacy row exists.

### Recommendation

Do **not** blindly delete it. First prove the database invariant:

- no non-revoked target-less grants exist;
- no supported rollback/version can create them;
- historical UI/API consumers do not require the old fields.

Then perform a data migration to a final historical representation (or archive/export if history is only operational evidence), remove runtime rejection paths, and drop the old columns.

This is one of the best examples of "legacy that should eventually disappear, but is currently doing useful fail-closed work."

---

## M6. `tools.py` keeps backward-compatibility re-exports primarily for old imports and patch targets

**Severity:** Medium  
**Removal safety:** 4/5  
**Removal simplicity:** 4/5

### Evidence

`services/langgraph/src/agents/po/tools.py` says it re-exports tools from submodules "for backward compatibility" and explicitly preserves old test patch targets such as:

```python
patch("src.agents.po.tools._get_api", ...)
```

The real implementation is already split across `tools_briefs`, `tools_projects`, `tools_shared`, and `tools_stories`.

### Why it matters

The facade makes old import topology part of the de facto API and lets tests pin implementation paths that the production code no longer conceptually owns.

### Recommendation

Keep `get_all_tools()` as the public composition point, but update internal tests/importers to patch/import the real owner modules. Then remove compatibility re-exports that are not part of the intended public surface.

This should be easy to do with code search and is unlikely to affect runtime behavior.

---

## M7. ConfigStore's stale-last-known behavior is a deliberate fallback that conflicts with the global fail-fast doctrine

**Severity:** Medium  
**Removal safety:** 2/5  
**Removal simplicity:** 4/5

### Evidence

`shared/config_store.py::_source_unavailable()` returns an expired cached value when the config API becomes unreachable or returns an invalid response.

The docstring makes the policy explicit: already-running callers keep running on their last known value.

### Why it matters

This behavior may be operationally correct, but it is an exception to `AGENTS.md`'s broad "fail fast / no fallback values" rule.

The problem is less the code than the policy ambiguity: future contributors cannot know whether "last-known config" is approved resilience or technical debt.

### Recommendation

Pick one of two directions and document it as an invariant:

- **Fail closed:** no stale reads; config-source failure stops the operation/service.
- **Last-known-good:** stale reads are explicitly allowed for named operational settings, with a maximum staleness bound and metrics/alerts.

I would prefer the second for polling intervals and health thresholds, but not for security/admission/budget policy. A single ConfigStore policy for both categories is too coarse.

So this item is more likely to be **refined** than simply deleted.

---

## M8. Frontend builds rely on `npm ci --legacy-peer-deps`

**Severity:** Medium  
**Removal safety:** 4/5  
**Removal simplicity:** 4/5

### Evidence

Both:

- `services/admin-frontend/Dockerfile`
- `services/user-dashboard/Dockerfile`

run:

```dockerfile
npm ci --legacy-peer-deps
```

### Why it matters

This globally disables peer-dependency enforcement for reproducible production builds. It usually means the lockfile currently encodes a dependency graph that modern npm considers inconsistent.

Unlike intentional runtime fallbacks, this is almost pure package-management debt.

### Recommendation

Run plain `npm ci`, inspect the actual peer conflicts, update/replace the offending packages, regenerate the lockfiles, and remove the flag.

Do both frontends in the same cleanup if they share the same dependency cause; otherwise split them.

---

# Low severity

## L1. Makefile keeps explicitly named compatibility aliases and a legacy aggregate

**Severity:** Low  
**Removal safety:** 5/5  
**Removal simplicity:** 5/5

### Evidence

`Makefile` contains:

- `test-live-mega` as a "Temporary compatibility alias" for `test-live-mega-noop`;
- `test-live-pipeline` as a "Legacy aggregate, not a named suite".

`tests/live/README.md` says the legacy aggregate intentionally remains until duplicate coverage is removed later.

### Recommendation

This is exactly the kind of shim that should have an expiry condition.

For each alias, identify callers in CI/docs/scripts. When only humans/docs remain, update them and delete the alias. The aggregate can go when duplicate suite coverage is removed.

No architectural redesign is needed.

---

## L2. Legacy live-test prefix `mega-test` remains sweepable

**Severity:** Low  
**Removal safety:** 4/5  
**Removal simplicity:** 5/5

### Evidence

`shared/live_contour.py` gives the production contour:

```python
legacy=("mega-test",)
```

The prefix participates in cleanup ownership but not new stand resource creation.

### Recommendation

Query/clean old resources carrying that prefix, confirm no current test producer uses it, then delete the prefix and its tests.

Until that operational check is done, keeping it is cheap and safer than leaking old live-test resources.

---

## L3. External HTTP clients frequently create a new `httpx.AsyncClient` per request

**Severity:** Low  
**Removal safety:** 4/5  
**Removal simplicity:** 3/5

### Evidence

Examples include:

- `shared/clients/github/_base.py`;
- `shared/clients/time4vps.py`;
- `services/infra-service/src/provisioner/bitlaunch.py`;
- `shared/clients/embedding.py`;
- `shared/clients/registry.py`.

Some other clients (`InternalAPIClient`, worker broker) correctly keep a long-lived client.

### Why it matters

Per-request clients throw away connection pooling and make timeout/retry/resource ownership inconsistent. This is mostly performance/cleanliness rather than correctness at current scale.

### Recommendation

Give long-lived client classes one owned `AsyncClient` and an explicit `close()` / async context lifecycle. Do not introduce a global singleton.

---

## L4. Base settings policy and actual defaults are not fully aligned

**Severity:** Low  
**Removal safety:** 3/5  
**Removal simplicity:** 3/5

### Evidence

`AGENTS.md` says required environment variables should never have default values and presents this as a broad project convention.

At the same time, `shared/config.py` intentionally defines defaults for service name, log format, log level, and default agent type, while compose has several `${VAR:-default}` values for agent types, logging, host paths, and optional credentials.

Many of these defaults are reasonable; the issue is that the written rule sounds stronger than the actual architecture.

### Recommendation

Narrow the rule:

- **required identity/security/connectivity config:** no defaults;
- **safe local ergonomics / non-security operational config:** defaults allowed if documented;
- **behavior-changing production policy:** prefer system config or explicit env, not hidden defaults.

This is primarily a documentation/policy cleanup so future "remove all defaults" cards do not accidentally delete useful ergonomics.

---

# Legacy-looking code that should NOT be removed blindly

The following items look like exactly the "shim/fallback garbage" this audit was asked to find, but they currently have a defensible purpose.

## K1. Legacy `user_id` recipient rejection is a tombstone, not a compatibility decoder

**Keep for now.**  
**Eventual removal safety:** 3/5  
**Eventual removal simplicity:** 4/5

`shared/contracts/recipient.py` does **not** translate old `user_id` payloads into the new format. It rejects them fail-closed, because the field historically meant two different identities and Pydantic would otherwise ignore it as an unknown field.

The Redis client / Telegram proactive consumer also alert on this condition.

This is a good migration tombstone. Remove it only when all of the following are true:

- no old producer version can publish the field;
- Redis retention/PEL guarantees no old message can still be delivered;
- rolling downgrade is not supported.

At that point, delete the special alert path and let the ordinary DTO extra-field policy own malformed messages.

## K2. `CLAUDE.md` compatibility entrypoint is a tool-discovery shim

**Keep unless Claude Code no longer needs it.**

`CLAUDE.md` points to canonical `AGENTS.md` and deliberately does not duplicate instructions. That is a low-cost compatibility file at an external tool-discovery boundary, not runtime legacy.

Deleting it would save almost nothing and could make one coding-agent harness stop discovering repository instructions.

## K3. Repo-token → org-token fallback during repository deletion is narrow and evidence-driven

**Keep.**

In `shared/clients/github/_repos.py::delete_repo()`, a repo-scoped installation lookup returning 404 causes a retry with the org installation token. The code explains the concrete race: a newly created repo may exist before repo-scoped installation binding is visible.

This fallback is typed by HTTP status and scoped to one operation. It is qualitatively different from broad exception/string fallbacks elsewhere in the GitHub client.

## K4. ConfigStore last-known-good may be worth keeping for non-critical operational tuning

**Do not delete globally without classifying config keys.**

For polling/observability settings, last-known-good is reasonable resilience. For admission, budget, authentication, or security policy it can be dangerous. The cleanup is to make the distinction explicit, not necessarily to eliminate every stale read.

---

# Suggested cleanup order

## Phase 1 — safe, localized removals

1. Remove GitHub exception-string parsing and broad "return empty" behavior.
2. **Completed in PR #463:** remove the `worker-wrapper` private task-ownership hack; `main` now owns and cancels the run task directly.
3. **Implemented in PR #464 (pending merge):** make PO checkpointer persistence required in the production consumer path while retaining explicit `MemorySaver` construction for tests.
4. Remove PO summarization's broad ConfigStore → env fallback and log effective values.
5. Repair frontend peer dependencies and remove `--legacy-peer-deps`.
6. Remove obsolete Makefile aliases whose callers are already gone.

These are high-confidence changes with limited architectural surface.

## Phase 2 — compatibility deletion with proof

1. Migrate old Run observability consumers to the engineering ledger and drop compatibility projections/columns.
2. Prove no live target-less temporary-access grants exist, then remove the retired slot lifecycle schema and guards.
3. Prove no old recipient payload can remain in Redis, then remove the `user_id` tombstone.
4. Sweep old `mega-test` resources and remove the prefix.
5. Update tests/importers and remove PO tool compatibility re-exports.

The important word here is **proof**: these are easy to delete mechanically but unsafe to delete based only on code search.

## Phase 3 — architectural cleanup

1. Split scheduler runtime ownership into independently deployable entrypoints.
2. Separate live-test harness code from runtime `shared` code.
3. Clean `worker-manager` under the current root lint policy and unpin Ruff.
4. Normalize long-lived external HTTP client ownership.

This phase reduces future change cost more than it removes current bugs.

---

# Overall assessment

The codebase is not suffering from uncontrolled legacy accumulation. In fact, the recent architecture shows repeated attempts to replace implicit behavior with typed contracts and durable state.

The remaining debt has a recognizable shape:

- **transition residue**: old fields, aliases, tombstones, compatibility projections;
- **resilience residue**: broad fallbacks that survived earlier iterations even though the repository now prefers typed failure;
- **process-boundary lag**: modules have been separated more cleanly than runtime ownership has;
- **tooling lag**: `worker-manager` is still protected from the repo's current quality gate;
- **shared-package sprawl**: runtime and test-harness concerns coexist in one ambient dependency tree.

The best cleanup strategy is therefore not a rewrite. The repository is already close to the architecture it says it wants. The highest leverage is to **finish migrations that are already half-complete and make the runtime/process boundaries match the code boundaries that already exist**.