# Changelog

One `## YYYY-MM-DD` heading per merge day, newest first; one bullet of at most two lines per entry.
See the CHANGELOG rule in [AGENTS.md](../AGENTS.md).

## 2026-09-03

- A paid stand failure at QA now carries the QA and deploy Run records, the `qa-worker` and
  `deploy-worker` tails and, when a successful deploy left an unreachable URL, a target-host snapshot.
- The paid LLM stand suite now asks for a per-run marker field in the health payload, so the worker
  has something to commit, and it fails at once when the story branch is not ahead of `main`.
- A `stand_token` worker now reaches container creation: the manager asks its `ExecutorDiagnostics`
  collaborator for stand-token failures instead of a method it lost in the collaborator extraction.

## 2026-09-02

- A failed paid stand run's acceptance artifact now names the failing stage, the control-plane
  reason from its engineering Run records, redacted service tails and every debug dump, or why one is missing.
- The e2e debug dump is bounded and redacted before it is written, so nothing unredacted crosses the
  handoff now that the dump reaches the acceptance artifact instead of dying with the stand host.
- Removed the central QA API fallback (`QA_LLM_*`, the ReactAgent graph and its LangChain tool
  wrapper): QA has one executor, and a failure to start it is a typed terminal outcome.
- Removed the `worker_type` cutover migration and its startup hooks after a read-only Redis scan
  proved zero typeless worker records on both production and the stand.
- Removed `ScaffoldConfig` and `WorkerConfig.scaffold_config` from the worker command contract:
  worker-manager mounts workspaces by `repo_id` and never read the field.
- Removed `WorkerResultStatus.REJECTED`, `ServerStatus.MAINTENANCE`/`DECOMMISSIONED` and four
  engineering state fields nothing reads; the persisted-history enums and DTO fields were left alone.
- Removed `mypy` and its `[tool.mypy]` section: no Makefile target, workflow or script has ever
  run it.
- Documented the hand-installed stand session keepalive cron job in `docs/DEPLOY.md`, so the script
  is no longer unreferenced.
- Changed the CHANGELOG rule to one dated heading per merge day and one two-line bullet per entry,
  and applied it to the whole file, which halved its size without losing an entry.
- Third dead-code pass: dropped stale live tests, the `telegram_bot` legacy suite, the
  `shared/redis_client.py` shim and unread constants; comments now describe live behaviour.
- Retired the legacy `tests/e2e` contour and its compose stack: nothing invoked it and its
  mechanics no longer matched the contracts; its two unique invariants are kept as drafts.
- First dead-code pass: removed unreferenced scripts, env vars, deps, re-exports and DTOs, and
  made `docs/CONTRACTS.md` forbid compatibility shims in favour of Alembic migrations.
- The architect now declares a planned scheduled behaviour in the product's `jobs_schema` and
  authors the matching `FIRE JOB` criterion, so QA fires a name the product really accepts.
- Repinned `service-template` to `91e5821` so generated products carry the core settings and jobs
  contracts; the vendored env-contract fixture was regenerated from the same commit.
- QA can now fire a named scheduled behaviour of the product under test via `POST /jobs/fire` and
  read its evidence back, with the capability never entering the executor container.
- A brief-backed QA run now receives the confirmed `initial_settings` as typed data, so acceptance
  steps assert against the real values instead of re-reading the story prose.
- Confirmed brief settings now reach the deployed product through its own `POST /settings/set` with
  proved readback, recorded in `DeployRunResult.settings_seed` and the `settings_seed_failed` outcome.
- Added PO tools `present_product_brief` and `confirm_product_brief`, and `create_story` now refuses
  new product work without a confirmed brief, binding the brief before publishing.
- Added `ProposedProductBriefContent` for brief writes: path-safe requirement ids, sourced wording,
  and typed `initial_settings` that refuse credential-shaped keys and values.
- Corrected the `_release_planning_attempt` docstring: `supervise_stuck_stories` scans only
  `CREATED`, so an incomplete plan needs an operator. No behaviour change.
- The architect consumer now claims a Product Brief planning attempt, heartbeats it, records
  per-requirement coverage and admits the brief once, so plans are released as a whole.
- Documented the Story/Task lifecycle owner, the dispatch admission point and `waiting_on` across
  `CONTRACTS.md`, `PIPELINE_V2.md` and `ERROR_HANDLING.md`, correcting three stale passages.
- A planning-attempt takeover now voids the superseded plan in the same transaction, and
  `complete_stories` ignores cancelled tasks, so a recovered story can finish.
- `supervise_stuck_stories` now retries a brief-backed story whose plan is incomplete and
  unowned, instead of skipping every `created` story that already has tasks.
- Admission now refuses a task whose `tasks.dispatch_admitted` is false with the non-overridable
  `product_brief_not_admitted`, making brief admission the coverage-to-dispatch boundary.
- Added `ProductBrief` and `RequirementCoverage` (`b7c1e4a90d23`) with an idempotent `admit` step
  and planning-attempt fencing, so exactly one architect can release a plan.
- `PATCH /api/tasks/{id}` now freezes `project_id`, `story_id` and the planning attempt of an
  unadmitted task, and `admit` fails closed when a disposition names a task outside the plan.
- `tasks.dispatch_admitted` is non-nullable defaulting to true (the migration backfill), and both
  `TaskDTO` and `TaskRead` require it so an omitted field is never read as dispatch authority.
- Declared `LOCK_LADDER` in `engineering_dispatch_admission.py` and take every condition row
  through it; a story roster that grew is refused with `story_roster_changed`.
- `spawn-worker` now peeks `Task.status` column-only, so SQLAlchemy's identity map can no longer
  hand admission a stale, unrefreshed Task row under its `SELECT ... FOR UPDATE`.
- `POST /work-admission/paid-runs` refuses an `engineering` command naming an existing Task with
  `engineering_task_dispatch_requires_admission`, keeping Task dispatch to one admission point.
- Gave paid engineering dispatch one admission point over
  `POST /work-admission/engineering-dispatches`, with typed refusals wrapping `start_paid_run`.
- The prior-attempt fence now names an `EngineeringDispatchRepair` for the dispatcher to execute
  instead of transitioning the task itself, so a decision carries no hidden side effect.
- `dispatch_todo_tasks` holds no admission condition: it asks the admission point per task and
  acts on the typed answer, keeping only message building on the scheduler.
- The admin `spawn-worker` route now goes through admission with audited `overrides`, so an
  operator spawn can only walk past `task_not_dispatchable` and `live_attempt_in_flight`.
- The story fence now also refuses when a sibling holds a queued or running engineering run, so a
  published task stuck in `todo` still reports `story_busy`.
- The blocker task row is taken with `get_task_for_update` in ascending task id, so a concurrent
  legal transition is not lost and reciprocal dependencies cannot deadlock.
- Added typed `StoryWaitingOn` and the non-nullable `stories.waiting_on` column (`c3f7a91d2b48`),
  written only by `_land_on` and exposed through `StoryRead`, `StoryDTO` and the admin overview.
- Made multi-hop Story moves declared server-side actions validated and committed in one transaction,
  starting with `POST /stories/{id}/retry-after-ci-failure`, so callers stop chaining transitions.

## 2026-09-01

- Serialized Story and Task writes in `services/api` with `SELECT ... FOR UPDATE` helpers and
  per-hop transition validation, so concurrent callers on one row can no longer both win.
- Moved the Stand E2E one-shot health probe into a unit-tested scheduler module that initializes
  config before calling the canonical health checker.
- Fixed the Stand E2E health timing record so its JSON cannot terminate the surrounding
  single-quoted SSH payload.
- Consolidated assistant guidance in `AGENTS.md` with `CLAUDE.md` as a thin entrypoint, dropped
  obsolete repo-local pipeline files, and added a root repository map.
- Gated Stand E2E on a complete GHCR worker release for the exact workflow SHA, failing before
  billable infrastructure instead of building worker images locally.
- Added the request-scoped `stand_e2e` provisioning profile that skips `dist-upgrade` for the
  disposable target, reports timing phases, and health-probes before allocation.
- Made dynamic target provisioning failures diagnosable before pytest: the observer outlives
  provisioner budgets and failed setup keeps redacted infra and scheduler log tails.

## 2026-08-31

- Moved test-only Dockerfiles and Compose harnesses from root `docker/test/` to the test-owned
  `tests/compose/`, updating make targets, CI and docs without changing suite behavior.
- Made stand evidence collection suite-aware so single-cell runs fetch only their own logs,
  removing three guaranteed missing-file SCP waits per run.
- Preserved live worker failure attribution across stand teardown by transferring bounded redacted
  `run-evidence` JSON (schema v5) into the acceptance artifact.
- Completed the free `mega-noop` acceptance tail with dependency-ordered noop Tasks and per-Run
  audit, executor, reservation and zero-cost ledger proofs; cap raised to 75 minutes.
- Fixed per-application undeploy to release only that application's port allocations in the same
  transaction as its `not_deployed` status, idempotently and without touching history.
- Replaced broad stand control-plane provisioning with a minimal builtins-only bootstrap keeping
  SSH, Docker, pinned `uv` and fail-closed postconditions, to cut the ~7 minute setup.
- Fixed the `mega-noop` completion-event matcher for the PO subject contract, where story-level
  owner notifications carry the story id in `task_id`.
- Extended `mega-noop` acceptance to the full lifecycle: health probe, `Story.completed`, owner
  record and PO event, merged SHA, and undeploy through `Application.not_deployed`.
- Defined the canonical stand E2E suite contract mapping `mega-noop`, `mega-llm` and `matrix` to
  exact pytest nodes with derived timeouts, keeping legacy aliases working.
- Regenerated the vendored service-template fixture from pinned commit `edf54dfb` with its Copier
  answers and a whole-render digest, so a stale render cannot pass provenance checks.
- Fenced temporary QA capability calls on current Run and lifecycle state, withdrew superseded
  grant dispatches, and pinned production scaffolding to generated-service commit `edf54dfb`.
- Replaced the temporary Telegram environment slot with a durable capability-backed QA lifecycle
  requiring active/inactive readback; legacy slot records now fail closed.
- Hardened temporary QA access recovery so contention defers only the affected handoff, stale
  operations consume bounded retries, and exhausted cleanup escalates once.
- Fixed the temporary QA grant-attempt Alembic revision to extend the capability-target migration
  instead of colliding with an existing revision.
- Classified temporary QA records at the API boundary so legacy id collisions fail closed, and
  cancelled deploy-lock or fence operations redispatch without consuming proof budget.
- Centralized runtime and deploy diagnostic redaction so deployer, GitHub-secret and smoke
  surfaces strip secrets, dotenv content, auth values and bot tokens before they are recorded.
- Fenced automatic initial-owner grant-intent rebinding inside the API admission transaction, so a
  live or exhausted intent returns its durable disposition instead of publishing another Run.
- Bounded grant-intent admission by the deploy retry ceiling per target or user-retry epoch, so
  explicit retries can resume an intent without reopening automatic recovery loops.

## 2026-08-30

- Reworked permanent generated-service grants into API-owned durable intents with immutable deploy
  attempts, stale-target rebinding, typed per-call dispositions, and supervisor recovery.
- Replaced generated Telegram bot audience configuration with the `users.grant` capability,
  injected as a generated secret and requiring owner grant plus active readback after smoke.
- Added durable typed `users_grant_intent` deploy metadata that persists grant dispatch before
  publish and records access only after the generated service reports the identity active.
- Read durable grants from the generated `UserAccess` `status` field, fail closed on malformed
  responses, and apply the owner seed intent once so retries rebind instead of redeploying old SHAs.
- Split the projects API router into lifecycle, secrets, bot, Telegram, and teardown domains,
  keeping public routes and moving access checks into the canonical guards module.
- Moved executor diagnostics and worker teardown evidence out of `WorkerManager` into explicit
  collaborators, preserving lifecycle ordering and the manager's public facades.
- Split scheduler supervision into liveness, deploy, QA, handoff, and shared-support modules while
  keeping the runtime facade and behavior unchanged.
- Removed dead provisioner-graph state, test-only delegates, router re-exports, phantom worker
  metadata, and redundant Compose replicas.
- Replaced the field-by-field contract copy with a compact registry pointing at canonical REST and
  queue sources, owners, delivery rules, and lifecycle fences.
- Merged equivalent offline live-harness scenarios and dropped duplicate setup, keeping cleanup,
  evidence, ownership, and authentication coverage.
- Deleted historical planning and incident docs and reconciled living documentation with current
  pipeline, QA, secret, worker, and lifecycle behavior.
- Trimmed historical source prose down to current lifecycle, security, and failure invariants with
  no behavior change.
- Removed unreachable LangGraph schemas, obsolete SSH helpers, aliases, and unused scaffold code,
  keeping the live allocator, HTTP health probe, and BitLaunch policy boundary.
- The stand-only Definition-of-Done restart target identifies terminal adoption by its fenced turn
  request instead of attempt counts, so ordinary retries no longer redden the restart proof.
- Hardened the stand-only Definition-of-Done target against unrelated PO input, cold worker
  creation, and terminal worker history, with acceptance driven through public story actions.
- Added the stand-only Definition-of-Done live target covering owner completion, audited
  `accept-result`, and `stand_token` restart, proving retained turns and no detached worker state.
- Operators can drain the engineering consumer from the admin Workers page before a deploy; the
  audited drain survives restarts and recreation, and worker inventory reports three-valued facts.
- Engineering consumer rollouts hand a reclaimed live turn to the replacement instead of publishing
  a second prompt, and terminal settlement tears down every unconsumed recorded turn.
- The main-only backend DinD integration compose now supplies worker-manager's internal API key so the
  service starts.

## 2026-08-29

- Custom Telegram bot audiences keep the verified sender and project owner unless the persisted
  `allow_ownerless_audience` opt-out is set; the PO confirms one audience summary before a story.
- Operators can recheck a repaired QA infrastructure blocker from `waiting_human_review`; the
  audited, idempotent action returns through deploy so the post-repair route has one provenance.
- Operators can accept a `waiting_human_review` story from the admin UI with a required, audited
  basis; acceptance refuses when the handoff's application is not running and points to Recheck QA.
- Completing a story writes its durable `story_completed` PO notification in the same transaction,
  so recovery redelivers it via `po:input` with the verified URL and bot username.
- `python -m shared` is the canonical broad unit suite entry point, running the tree's own script so
  the `check broad --reuse` receipt records in-tree import provenance and can be reused.
- API actor guards now resolve the caller from the request credential, so `X-Telegram-ID` names an actor
  only for internal-key services and ownership/allocation checks use that principal.
- Stand image resolution falls back to a local build only on GHCR 404; Factory always sends its API key,
  and scheduler cadence follows the allocation freshness policy.
- Registry token and release-marker lookups share one response matrix that separates tooling from
  transport errors and never authorizes a build on auth, rate-limit, or unexpected responses.

## 2026-08-28

- Stand e2e runs the selected suite via `scripts/stand_run.py` and builds one redacted acceptance
  artifact with cleanup inventory and run cost, failing closed on leaks or incomplete cleanup.
- The ephemeral stand authenticates workers by a non-secret `stand_token` selector, resolving Claude
  and Codex credentials locally with validated expiry and failing closed when unavailable.
- Stand e2e bootstraps its dynamic orchestrator from the checked-out revision and registers a
  pending BitLaunch target bound to its provider ID, dropping the `stand-self` script and target.
- Destructive server operations follow the provider-owned provisioning policy: rows need an explicit
  provider label and stable ID, and are never adopted by IP.
- Stand e2e creates a bounded, run-owned BitLaunch orchestrator/target pair through a
  provider-neutral lifecycle with balance/quota preflight and tagged cleanup, dropping static hosts.
- Failed worker creations keep their terminal status, ownership, and workspace fence until `delete_worker`
  confirms removal; image fallback runs only for a confirmed missing release.
- Stand acceptance admission rejects private-key PEM markers in allow-listed evidence, including escaped
  and serialized headers, while still permitting value-free token-preflight diagnostics.
- Redaction needles now come only from a fixed protected-value allow-list, and both artifacts use an
  `always()` admission gate so failed runs keep diagnostics while bad evidence still blocks upload.
- Stand acceptance evidence installs the pinned `uv` runner via dynamic host provisioning and runs the
  suite only after provisioning; artifacts are attempt-scoped and gated on a value-free scan.
- Stand cost evidence keeps BitLaunch's per-server `rate` and labels rounded-hour estimates, reporting an
  actual charge only for an exactly correlated usage row.
- Stand token validation binds runner or rendered stand-host credential names before expiry checks, so
  `make stand-preflight` and `make stand-run` stop failing on valid `STAND_*` config.
- Provisioner writes every SSH private key with exactly one terminal LF before Ansible, keeping keys usable
  when secret storage drops the final newline.
- Deployment maps the Time4VPS secret to the provider-scoped policy key, and scheduler discovery refuses IP
  collisions with a critical alert instead of creating a shadow server.

## 2026-08-27

- The admin Dashboard reads one strict internal overview contract reporting queue bindings, task
  counts, paid work, and bounded failed-Run errors, marking malformed snapshots unavailable.
- Worker-manager publishes credential-safe Claude and Codex availability snapshots to Redis; paid
  admission refuses proven-unavailable executors and needs confirmation for `unknown`.
- The main-only backend DinD suite gives worker-manager the same test-owned Claude host-session volumes and
  fake refresh credentials, so integration workers reach their lifecycle tests.
- Queue inspection marks incomplete Redis consumer-group observations as degraded instead of inventing zeros,
  and the Dashboard gains an empty state plus a recursive admin contract gate.
- Unknown executor-diagnostic confirmation now requires an admin LK bearer, and terminal pre-container worker
  refusals settle as zero leases while lifecycle disagreements still fail closed.
- Executor diagnostic records reject contradictory enabled, auth-mode, availability, lease and reason states,
  and worker-manager reconciles Redis against Docker before reporting lease counts.
- Executor diagnostics require a complete protocol version, map expired snapshots to typed unknown, and re-read
  a confirmed unknown right before paid admission so a stale confirmation cannot be reused.
- Executor-availability fixtures publish complete fresh diagnostics, the rollout suite survives control-plane
  replacement, and Settings updates stale state outside render.
- Deploy seeding initializes absent paid-work controls through a distinct typed operation and never overwrites
  live emergency-stop, ceiling, or override values.

## 2026-08-26

- Admin Settings exposes one typed, audited paid-work control state — emergency stop, run ceiling,
  and engineering/QA executor overrides that apply to new Runs without a restart.
- Paid engineering and QA runs get an immutable typed executor decision at admission, read by
  launchers via Run id so later configuration changes cannot switch a queued attempt.
- Telegram `/balance` shows a user's ledger-derived spend and available amount, warning on incomplete
  cost coverage, without exposing the reservation split.
- PO gets a self-only balance tool and must check it before paid work, so a single-attempt reservation
  cannot exhaust the budget or start work the gate would reject.
- Production admin access binds only to `127.0.0.1:3001` behind SSH forwarding, and analytics, deployment and
  queue-debug routes require an administrator or internal service.
- Paid-run test fixtures use the transactional paid-run command; fixtures that only need a Run record use a
  non-paid type.
- Paid-run starts replay an identical command idempotently and reject conflicting payloads; the standalone
  admission oracle is gone and admission controls are shielded from the generic config API.
- Paid-work refusals persist their command identity, project owner, typed reason and Russian owner-facing text
  instead of caching a transient outcome.
- A paid-run retry rechecks controls rather than caching a prior refusal, reusing a live Run only while its
  engineering reservation is active.
- Scheduler admission refusals park the Task as well as the Story, and a handled pre-handoff publish failure
  closes the Run and releases its hold.

## 2026-08-25

- One-time promo-code registration that atomically arms an enabled engineering budget policy.
- Count-based admission for projects and concurrent runs, with an admin emergency stop and audited
  decisions; paid runs now start via one transactional command that checks money after the count gate.

## 2026-08-24

- A repeated deterministic dispatch no longer replays a released reservation's old `admitted` verdict; it
  re-evaluates ledger spend and holds under the policy lock, so a deploy-fix retry keeps its hold.
- Supervisor deploy-fix redispatch now takes the same durable budget admission before creating a Run; a denial
  writes a `story_quarantined` owner notice and parks the story in human review.
- PO owner-notification events now share one typed vocabulary, so `POSystemEvent` rejects unknown names and a
  durable notice cannot be marked delivered for an event PO would drop.
- Dispatch recovery treats queue publication as the only handoff boundary: pre-publish failures release holds,
  and scheduler budget denials move tasks to `waiting_human_review` with balance context.
- Engineering dispatch now makes a durable, server-authoritative budget admission before creating a Run, with
  per-user policy locks, zero-budget denial, and stable decisions for repeated attempt identities.
- Added durable per-user engineering-budget policies and a ledger-derived balance API, using integer micro-USD
  limits, optimistic versions and idempotent admin writes instead of a mutable spend counter.
- Factory result normalization now derives totals only from valid usage components, so malformed usage cannot
  reject a terminal ledger write; evidence with no reported model keeps the configured one.
- Factory `droid exec -o json` terminal results now carry typed model, token and cache facts to the ledger;
  money-looking fields are discarded, so Factory and Codex attempts keep explicit unknown cost.
- Claude terminal results now carry one typed provider-evidence object to the ledger, with money parsed as
  `Decimal` into micro-USD; invalid monetary evidence is explicit unknown cost, never zero.
- Added the append-only engineering-attempt ledger: terminal Run updates write one idempotent record under the
  Run row lock, exposed read-only at `GET /runs/engineering-attempts`.

## 2026-08-21

- Telegram bot audiences can now be changed one user at a time via typed add/remove endpoints under the project
  row lock, because `set_bot_access` replaced the whole list and an LLM could silently drop IDs.
- Bot-audience rollout hardening after review: `set_bot_access` reaches live containers, rollout targets and
  status reads are project- and owner-scoped, and a durable publish-intent record closes the commit/publish gap.
- [hotfix] A manual CI dispatch for `main` now runs the required backend Docker-in-Docker suite, since the
  release gate refuses a skipped DinD result and the job had been limited to `push`.
- [hotfix] Worker images now ship the `shared.constants` module `worker-wrapper` imports, which had made every
  worker crash with `ModuleNotFoundError` before its agent CLI could start.
- [hotfix] A backend Docker-in-Docker failure can no longer release worker images: the main-only runtime suite
  moved into the `ci.yml` DAG behind `Required CI Gate`, so a failed job leaves the SHA unreleased.
- Engineering worker supervision now follows the input lease rather than a heartbeat: the broker records the
  active turn, missing Redis status is unknown rather than death, and retry waits for confirmed removal.
- The PO consumer now reads `po:input` through `RedisStreamClient.consume_typed` instead of a private
  `XAUTOCLAIM` copy, inheriting the continuous sweep, DLQ quarantine and lost-entry diagnostics.
- A stream entry no longer disappears without a trace in `shared/redis/client.py`: PEL reclaim is continuous,
  poison entries are quarantined to a DLQ, trimmed entries are diagnosed, and unknown fields are tolerated.
- Re-delivering a scaffold message onto an already-scaffolded workspace now succeeds: "nothing to commit" is
  told apart from a real commit failure via `git status --porcelain`, so the push still runs.
- A failed scaffold no longer fails every story of the project; `_process_full_mode` fails only stories still in
  `created` and leaves in-flight or finished work alone.

## 2026-08-20

- [hotfix] Ensure-workspace jobs now clone the linked Repository name instead of rebuilding one from
  `Project.slug`, which made imported projects report a missing repository and stall every story task.

## 2026-08-15

- [hotfix] The PO-default matrix preflight now binds its PO tool to the checkout it is proving, since `uv run`
  put `services/api` first on `sys.path` and `import src.agents...` resolved to the wrong service.
- The Production Agent Matrix now runs a PO-default preflight that calls `create_project` with no `agent_type`,
  checks the API's runtime default, and retains a SHA-bound redacted receipt with cleanup on every path.
- PO-default preflight notification evidence now takes a `po:proactive` stream boundary before creating either
  project and examines only the post-boundary delta, so historical deliveries cannot poison a dispatch.
- The Backend Docker-in-Docker Claude-agent checks now own each worker through create, liveness and deletion; a
  stopped worker reports status, exit code and a log tail instead of retrying a Docker 409.
- Application undeploy now streams one project-scoped cleanup script that captures Compose-labelled artifacts,
  verifies their removal, and retains a candidate snapshot so a referenced anonymous volume is retryable.
- QA's Telegram capability now returns typed reply evidence (text, caption, media, keyboards, callback answers,
  post-press edits); callbacks are accepted only for buttons the run observed, and QA gets no credential.
- A Telegram capability error now overrides the agent's verdict with a typed QA blocker: proven-undelivered is
  `telegram_probe_undelivered`, ambiguous is `unknown`, and only clean failed checks become engineering fixes.

## 2026-08-13

- The Backend Docker-in-Docker suite reaches its assertions again: path-loaded `tests/live` modules are now
  registered in `sys.modules`, without which `@dataclass` raised before any test body ran.
- The orphan sweep in `garbage_collector.py` now keeps a container that is running, paused or restarting with no
  Redis entry, since a lost `worker:status:*` had made it delete live workers mid-run.
- Run cleanup is driven by ownership labels rather than a reconstructed context: `run_cleanup.py` removes a run's
  containers, sidecars and networks by `com.codegen.run.id`, idempotently, and verifies afterwards.
- The label is the fence as well as the finder: a resource whose `com.codegen.run.id` is not this run is refused,
  and long-lived service containers carry no run label, so cleanup cannot take a neighbour's resources.
- `dev_proj_<worker_id>` networks now carry the worker's ownership labels plus `com.codegen.type=worker-dev-network`,
  because a worker id is exactly what is unrecoverable once its container and metadata are gone.
- A retained `worker:meta:<id>` is removed only once the run's evidence accounts for that worker; otherwise it is
  kept and reported as expected residue, and a run with no evidence takes a capture pass first.
- A run has one evidence artifact that only ever gains: `retain_evidence` merges into it rather than replacing,
  keeping the fuller record, since a second recovery pass runs after the first pass's sources are gone.
- Removal is fenced by accounting, not by a capture attempt: `clean_run` leaves in place anything the evidence
  does not name, and a failed capture must be recorded as a stated missed capture to authorise removal.
- Crash recovery now cleans in four phases — fence, capture, remove, verify — establishing the cancellation fence
  first, since a sweep ahead of it would force-remove workers of a still-live run.
- The CLI cleanup adapter a crash recovery actually uses is now proved against a real daemon, since its Go
  templates had only ever been answered by fixtures and a misrendering would give a false all-clear.
- The backend Docker-in-Docker suite now runs on every push to `main` via `backend-integration.yml`; it is the
  only real-daemon proof of label attribution and ran only when someone remembered to dispatch it.
- Whoever removes a worker captures its ending first: `delete_worker` writes exit code, redacted log tail, image,
  agent type and transcript path to `worker:evidence:removed:<run id>` before the container goes.
- Destructive steps are ordered by how much attributability they destroy: the container always goes, but
  `worker:meta:<id>` is deleted only once the removal record exists, else it is retained and logged.
- Capture never owns cleanup: it is bounded by `WORKER_REMOVAL_EVIDENCE_TIMEOUT_SECONDS`, raises nothing, and
  turns each unreadable fact into a stated reason so a worker is still removed.
- The run evidence collector (artifact schema v4) reads three sources by strength: live labelled containers,
  removal records, then the ownership manifest, which can only add an explicit missed capture.
- Every worker/QA combination of the production matrix now emits one retained artifact naming the SHA, image
  digests, project, agents, attempts, terminal state and per-container exit code and log tail.
- Discovery is now a `docker ps -a` label query, so a worker that exited with its metadata already deleted is
  still listed; the QA creation-window heuristic and container-name prefix are gone.
- Because a label cannot survive container removal, evidence is collected on every engineering and post-deploy
  QA poll before cleanup, with the ownership manifest reconciled in as a second source.
- A QA role counts as exercised only once its worker handed a result to QA, and the executor is reported from the
  container actually observed rather than the qa-worker's configured selector.
- The privacy boundary is unchanged: Codex CLI diagnostics stay out of results and logs, the artifact's log tail
  is bounded and redacted, and the transcript is referenced by path rather than copied.
- Every dynamic worker is stamped with its owner at creation: `WorkerConfig` requires `WorkerOwnership`, written
  to Docker labels and `worker:meta:<id>`, so a worker that dies immediately is still attributable.
- The QA executor is ownable on the same terms, with its egress proxy labelled by run; its isolation is unchanged,
  and because it now records a project it is explicitly excluded from the developer workspace mutex.
- A worker's run is the run that initiated the work and enters the system only via `Project.initiating_run_id`,
  carried on engineering and QA messages so a live run can find its own dead workers by label.
- The attempt is carried separately as `com.codegen.attempt.id`, the Run row a worker was spawned by, because one
  initiating run may spawn many attempts and `com.codegen.run.id` never carries an attempt id.
- The live harness names its run before creating anything: `OwnershipManifest.run_id` is a fresh `live-…` identity
  used to create the project and to name the manifest file.
- Projects predating run ownership are never backfilled, since any substitute id would mislabel their workers;
  absence raises `ProjectPredatesRunOwnership`, so such a project cannot dispatch engineering or QA work.
- A developer worker's ownership is stamped just before the `SADD` that takes the project and withdrawn if that
  `SADD` loses, so racing creates cannot share a checkout and a refusal is terminal in Redis.
- Worker base images are one immutable release chain keyed by the git SHA: every green `main` commit publishes
  common plus the claude, codex and factory images to GHCR under that SHA, with no mutable `:latest`.
- A release is one registry write: a final `worker-base-release:<sha>` marker carrying the digest record is
  published once all four images resolve, so a failed run leaves inert residue and a rerun completes the SHA.
- The deploy consults the release marker first and refuses a revision with no release (exit 9) before any local
  tag moves, and refuses without repairing a release whose image is missing or carries a wrong source hash.
- The deploy records the digests it actually verified: the pull half resolves each tag once, retags from the
  digest and writes the record on the host, which the deploy copies back rather than re-resolving.
- The production deploy pulls worker images by exact release with no fallback and verifies each source hash
  before `compose up -d`, recording the deployed SHA and verified digests in the run summary.
- Added a production acceptance matrix for the subscription-backed developer and QA executors: the live mega can
  select Claude or Codex and force exploratory QA, and the manual workflow runs all four combinations.

## 2026-08-12

- Updated developer-worker test guidance to use the generated project's supported
  `make tests` and `make test-integration` targets.
- Preserve cancelled QA outcomes without breaking the deploy dispatch boundary: a cancelled deploy
  without a result may still record its worker's first outcome or be superseded after its lease.
- QA run terminal transitions now record `completed_at` atomically with their verdict, including cancellation,
  and repeated deliveries preserve the first timestamp.
- A first QA terminal state is now authoritative even when cancellation has no result, and `PATCH /runs/{id}`
  ignores caller-supplied completion timestamps until it records that state itself.
- **Resolve project worker default at the API boundary (`codegen-orchestrator-1177`)**: the API records its
  current `DEFAULT_AGENT_TYPE` at creation instead of PO injecting Claude, leaving existing records unchanged.
- A central QA worker stays `STARTING` until worker-manager has installed its instruction, `TASK.md` and
  `/workspace/qa`, and the wrapper waits for those files, closing the creation-to-first-turn race.
- Made Codex the default central exploratory-QA executor, using its native `--skip-git-repo-check` for the empty
  ephemeral QA workspace; `claude` remains an explicit override.
- Worker bind mounts are prepared in the Docker daemon's mount namespace when the daemon is remote, via a short
  networkless helper, repairing the manual DinD suite without weakening the production worker's boundary.
- Failure to inject an agent instruction file or `TASK.md` now fails worker creation immediately, instead of
  ACKing the create command and leaving callers to discover a dead worker through 60-second timeouts.
- The legacy DinD harness now waits for asynchronous worker creation rather than treating acceptance as
  readiness, tolerates cold image builds, and uses isolated test credentials for Factory and Claude.
- The deterministic QA probes now classify an unanswering dependency by what was unavailable rather than by
  which call met it first, fixing two places that got this wrong.
- Telegram rate limiting a `getMe` is no longer read as a dead bot: only 401 and 404 are `bot_not_live`, and
  other answers take the infrastructure route, honouring `retry_after` up to `BOT_LIVENESS_MAX_RETRY_DELAY`.
- Docker not answering now ends at the same outcome from both callers: `resolve_capabilities` raises
  `QAContainerRuntimeError` and retries, so `server_unavailable` again means only never reaching the host.
- Container state and bot liveness are now established deterministically before the exploratory QA executor
  starts; previously a dead container cost a model call and bot liveness was not checked at QA time at all.
- `run_container_state_checks` reads `docker inspect` over the session the run already holds; an exited,
  restarting or unhealthy container fails QA as a product defect and no executor is started.
- Bot liveness is asked of the API, which holds the token: `GET /api/projects/{id}/telegram/liveness` calls
  `getMe` and returns `BotLiveness`, keeping the credential out of QA; a refused bot is a human blocker.
- The infrastructure half reuses the existing path: `QAInfrastructureFailure` now carries the blocker it becomes,
  and `_alert_admins_no_executor` became `_alert_admins_qa_infrastructure`.
- Added two blocker categories to keep repairs unambiguous: `qa_probe_unavailable` for a dependency that did not
  answer, distinct from `server_unavailable`, and `bot_not_live` for Telegram refusing the stored token.
- Established probe facts are handed to the executor as given: `build_qa_prompt` takes `established_facts` and
  replaces the container checklist line with one saying not to re-check it; `QAResult` gains no field.
- A terminal story outcome can no longer be observed without the owner's message being published or durably owed;
  a failed `po:input` publish used to lose it silently and abort the rest of the dispatcher tick.
- Added `shared/contracts/dto/owner_notification.py`, written to `run_metadata` before the transition commits, with
  states `OWED`, `DELIVERED`, `UNADDRESSABLE` (never retried) and `ABANDONED` after three failures plus an admin alert.
- Because the record is written first it is not evidence the transition happened: nothing is published until the story
  is read back in the recorded terminal status, and a record without it is `VOIDED` rather than delivered falsely.
- `services/scheduler/src/tasks/owner_notifications.py` is the single seam for all three terminal supervisor paths;
  exhausted QA fixes now also reach the owner as `story_quarantined` instead of telling administrators only.
- The two remaining terminal notifications (`_escalate_refused_deploy`, `_park_task_waiting_resources`) take that seam
  too, replacing `except Exception: log.warning` publishes; a task notice keeps its task id on the engineering run.
- The four publishes left in `supervisor.py` are non-terminal waits and resume notices; they stay direct and best
  effort, outside the guarantee, since each fires once and no scan re-derives them.
- `supervise_owed_owner_notifications` retries owed records from the new `GET /api/runs/owner-notifications/owed`,
  selected by state and not age, and `supervisor_cycle` gained five counters separating chasing from giving up.
- Delivery is at-least-once and bounded to the publish leg: a settled record is never published twice and an owed one
  is never forgotten; the Telegram transport keeps its own retry in `services/telegram_bot/src/proactive.py`.
- Regression tests drive the real ordering, both ways the transition and publish come apart, and the API selection
  rule that a month-old owed record is still work while a settled one is not.

## 2026-08-11

- The QA executor's CLI can reach its model backend again: the wrapper's replacement environment did not name the
  proxy variables, so every run ended `qa_executor_unavailable`; `QA_EGRESS_PROXY_ENV` now forwards exactly those four.
- The regression test asserts at the `create_subprocess_exec` boundary that worker-manager's variables and the
  wrapper's forwarded list are identical, so the two cannot drift apart again.
- Workers created before this branch survive the rollout: `shared/worker_type_cutover.py` marks typeless broker and
  worker-manager records `developer` once at startup, since a typeless record cannot be a QA worker.
- `test_control_plane_rollout.py` restarts the real broker and worker-manager the way a deploy does and proves an old
  credential still works while a QA one is refused; `docs/DEPLOY.md` now says both services must be rolled out together.
- A QA executor now has no control-plane authority beyond its own turn: its broker token is readable by the agent and
  was worth a compose build, which runs arbitrary `RUN` instructions on the management host outside the QA network.
- `shared/contracts/worker_control_plane.py` is an allowlist per worker type granting `qa` only the turn protocol;
  adding an operation without classifying it fails a test, so future routes are refused to QA by default.
- Enforced at both boundaries that hold the token — `worker-broker/src/main.py` and worker-manager's `compose.py` —
  reading the worker type from a server-side record; an unrecorded type is refused and developer workers are unchanged.
- An end-to-end regression against real services and a real Docker daemon shows a developer token building an image
  while an identical QA request is refused at both boundaries yet still runs its turn.
- "Exploratory QA cannot write to the application" is now a network property: the executor is attached only to the
  `internal` `codegen_qa_egress`, reaching the run capability endpoint, the broker and one per-run egress proxy.
- The proxy (`qa_egress_proxy.py`) speaks only `CONNECT` to the assigned CLI's model backend, so it cannot carry a
  `POST` and refuses the deployment with `403`; it is created and removed with the run, including by orphan GC.
- Fail-closed in `qa_egress.py`: worker-manager proves the network is internal, the proxy is listening and the
  container is on that one network, otherwise creation fails into the typed `qa_executor_unavailable` outcome.
- The runner's transcript and tool-trace write scan is unchanged, now a second layer over an enforced boundary rather
  than the boundary itself.
- `test_qa_egress_boundary.py` proves it against a real Docker daemon: no write requests reach a recording
  application from any client, with positive controls that the ledger, the capability endpoint and the tunnel work.
- Exploratory QA is performed by the assigned subscription agent again (Claude Code, or Codex via
  `QA_EXECUTOR_AGENT_TYPE`), started through the existing worker runtime as a repo-less `qa` worker.
- The injected `shared/qa_probe_cli.py` is the container's only route to the deployment, posting named calls to a
  per-run capability endpoint in `qa-worker` that dispatches into the same tool set the in-process agent used.
- `QA_LLM_*` is an optional API fallback read only after the assigned executor has failed; empty values are supported,
  and a transient executor failure is retried once while a broken session is not.
- With no executor and no fallback the run ends `qa_executor_unavailable` with an admin alert;
  `QABlockerCategory.CLAUDE_UNAVAILABLE` is removed because it had come to mean only "no LLM API key".
- The write guard also scans what the executor's container reported, since it has a shell, and `qa-worker` joins
  `codegen_worker` so the capability endpoint is reachable; `docs/DEPLOY.md` documents the reachability.
- Exploratory QA runs on `claude` or `codex` only, enforced both at config validation and in the `WorkerConfig`
  contract, since `factory` would use a provider API key and `noop` tests nothing; developer workers keep `AgentType`.
- Delivering the product no longer waits for temporary QA access to be handed back: `supervise_testing_stories` routes
  on the QA outcome alone, so a passed run completes its story on the next tick instead of stalling behind a revoke.
- The owner is told in the same tick with a `story_completed` event to `po:input` carrying the tested deployment's URL
  and bot `@username`; nothing had published that event since QA was decoupled from the story lifecycle.
- The cleanup sweep is unchanged and still revokes, reads back and retries, writing `qa_cleanup_failed` and alerting an
  administrator when its bounds are spent — now naming story, project, run and grant.
- A leftover test identity is a cleanup incident, not a failed product: only TESTING stories are routed, and story
  routing now runs before the access sweep so an exhausted cleanup cannot pre-empt routing.
- The sweep's counts separate `revoke_failed` from `escalated` on the `supervisor_cycle` line;
  `qa_waiting_for_access` is gone with the wait it counted.
- An admission refusal is no longer told as a memory shortage: a host in a non-admitting or unmanaged state fell
  through to the search path's last line and was reported as `insufficient_free_memory` on an empty machine.
- The refusal reason now lives beside the rejections as `shared/server_admission.py::ADMISSION_FAILURE_REASON`, raised
  by both placement paths, with a test comparing the two paths for every refusing state.
- No new vocabulary: `SERVER_NOT_PROVISIONED` still describes it, so its consumers are untouched and the disposition
  stays `INFRASTRUCTURE_WAIT` — never a capacity message to the owner and never a failed story.
- A request no managed server could fit even fully admitted still outranks it as `IMPOSSIBLE_CAPACITY` with
  `OPERATOR_REVIEW`, since waiting cannot make a small host bigger; the ordering is now pinned by tests and a comment.
- The QA identity is proved on the target before a host is recorded as having one: ownership is established by a
  root-owned marker the role writes, and an account found without it is refused rather than adopted or repaired.
- After configuration, `roles/qa_identity/files/qa-identity-proof` checks non-root uid, no privileged group, exactly
  one sudo rule and no access to the docker socket; a failed proof fails the phase so the label is never written.
- A target that lost the account after provisioning now reaches the provisioning journal as
  `qa_identity_absent_on_target` against the server handle, with the retrofit command.
- A refused retrofit is recorded as a `provisioning_failed` incident rather than only failing the command, and the
  per-host report names why each leftover stayed and how to remove it, including the untouched `/swapfile`.
- Provisioning now creates the QA identity: the `qa_identity` role makes `qa-observer` on every target and records
  `labels.qa_ssh_user` in the same call that marks the phase complete, with an empty supplementary group list.
- The QA runtime trusts `labels.qa_ssh_user` only as proof of provisioning, not as a target account: any value other
  than the one provisioning wrote is refused as `qa_identity_not_attested` before anything connects.
- The docker boundary moved onto the target: `/usr/local/bin/qa-docker` allows only read-only sub-commands, the QA
  account may run nothing else, and the runtime calls it through `sudo -n qa-docker`.
- The run connects as `qa-observer` rather than `ssh_user`; the fleet key is used only to add and remove the run's
  one-shot key, and `QASshGrant` records both accounts because the sweep connects as one and cleans the other's file.
- A fresh host provisioned the ordinary way now passes exploratory QA, where before a `server_sync` row defaulting to
  `root` was refused with `server_unavailable`.
- A target with no QA account is still refused, but visibly: journalled as a `provisioning_failed` incident with
  `details.step = qa_identity` so it reaches an administrator and the host stops taking new applications.
- Added `python -m src.provisioner.qa_identity_retrofit <handle>` for older hosts; it removes only what is positively
  the old runner's and leaves shared administrator paths and `/swapfile` alone, labelling only after success.
- Corrected `docs/DEPLOY.md`: servers provisioned by the current Ansible were not unaffected — the role configured
  `deploy_user`, which was `root`, and put it in the `docker` group.
- Made the QA grant sweep's walk survive the selection it drains: `offset` paging over a shrinking predicate slid open
  records backwards past the cursor, so a cycle could stop leaving a live `authorized_keys` line unreconciled.
- `GET /api/runs/qa-ssh-grants/held` now pages by `(created_at, id)` cursor, with half a cursor a `422`; `offset` is
  gone from route and client, so one cycle presents every record that was open when it passed.
- Stopped selecting the QA grant sweep's work by time: a 24-hour window put records left by a longer outage
  permanently out of reach, so `GRANT_SWEEP_LOOKBACK` is gone rather than widened.
- Added `GET /api/runs/qa-ssh-grants/held` so the selection is made on the record — every unreleased grant, oldest
  first — and `sweep_qa_ssh_grants` walks pages until one comes back short, so the page bounds only the response.
- An unparsable record stays visible instead of hiding or crashing the cycle: it is still selected, counted and logged
  as `qa_grant_sweep_unreadable_record`, because stopping there would strand every record behind it.
- Gave a central QA run one explicit capability set resolved from deployment data, replacing per-tool rules that were
  wrong on a shared host — machine-wide docker listings, any port, and lexical path containment a symlink could escape.
- Removed the host-wide command surface: `remote_exec` is now read-only docker sub-commands naming a container in the
  set, since `docker ps`, `df`, `uptime` and `journalctl` describe the machine and no capability can bound them.
- Made path containment physical: the read resolves on the target and checks membership after resolution in the same
  command, so a symlink cannot widen it; the secret-name check sits on top of that rather than instead of it.
- Made a target grant durable: `QASshGrant` is written to `run_metadata` before the key install, `RELEASED` only after
  readback, and `sweep_qa_ssh_grants` writes a `qa_cleanup_failed` blocker after three failed attempts.
- Stopped the revoke from being able to empty `authorized_keys` — the old form could copy an empty filter result over
  the fleet key's own file and lock the target out; it now refuses and hands the grant to the sweep.
- Refused exploratory QA on a target whose run identity would be root, blocking as `server_unavailable` rather than
  running privileged; health-only criteria never SSH and are unaffected.
- Moved exploratory QA off the deploy target: it is now a ReactAgent in `qa-worker` using the orchestrator's LLM and
  Telegram account, so a clean target with no CLI, credentials or Telethon session passes a full run.
- Replaced the agent's shell with a closed set of typed tools bound to one run (`agents/qa/tools.py`), each path,
  container and port checked against the run's single `QATarget` so naming another deployment is refused.
- Made the identity one-shot: each run mints an ed25519 key installed with `restrict` and an expiry, removed in
  `finally` and read back to prove it is gone; anything surviving becomes a `qa_cleanup_failed` blocker.
- Kept the write guard by removing what it guarded — no Bash and no tool takes an HTTP method — while the runner-owned
  tool trace is still scanned and a write found in it still quarantines the run.
- Dropped the `qa_runner` Ansible role from provisioning along with its 2GB swap, copied credentials and venv; older
  servers still carry it, nothing reads it, and removing it is a separate task.
- Removed `credential_refresh_loop`, which kept a Claude OAuth token alive on every managed server; there is no token
  out there to refresh.
- Added the `qa` LLM env group (`QA_LLM_MODEL` / `QA_LLM_BASE_URL` / `QA_LLM_API_KEY`); missing config blocks
  exploratory QA rather than producing a verdict, and the QA result contracts are unchanged.
- Closed the last way past admission: a project already bound to a server skipped the rule entirely, so a redeploy
  could land on a host whose provisioning had broken; the bound host now passes the same admission predicate.
- Refused reuse through the existing typed `AllocationError` with the admission budget, so it travels as a bounded
  infrastructure wait instead of reaching deploy as a `GIVE_UP` that fails the user's story.
- Bounded the resume-and-refuse cycle reuse makes reachable: the deploy wait now checks
  `supervisor.resource_wait_timeout_minutes` before admissibility, so a project pinned to a broken host reaches a human.
- Extended the shared admission matrix to the reuse shapes — allocations returned whole and a new module taking a port
  on the bound host — so all placement paths check the same state table.
- Gave every refusal disposition its own behaviour on both routing paths via `REFUSAL_ROUTING`; previously the deploy
  path answered all of them with one infrastructure wait, so an `OPERATOR_REVIEW` request was re-polled forever.
- Routed `OPERATOR_REVIEW` to the human-review queue on the deploy path, alerting operators and telling the owner an
  operator is needed; `StoryStatus.DEPLOYING → WAITING_HUMAN_REVIEW` is now a valid transition.
- Named `TECHNICAL_FAILURE`'s behaviour: a fleet the platform cannot see escalates to a human with an operator alert
  and no owner message, on both paths, without waiting or spending engineering iterations.
- Bounded the deploy infrastructure wait with `supervisor.resource_wait_timeout_minutes` and carried its start across
  re-dispatches in `run_metadata`; a refused deploy with no `head_sha` goes to a human immediately.
- Fixed story escalations that reached nobody: the supervisor posted `waiting_human_review` as a transition, which is
  a status and not a route, so the API answered 404; escalations now use the `human-review` action endpoint.
- Stopped an allocation refusal from failing a user's story on the deploy path: `DeployOutcome.WAITING_INFRASTRUCTURE`
  now carries the typed reason and admission budget, so the story stays DEPLOYING and is re-dispatched.
- Put the deciding rule in `shared/allocation_disposition.py`, classifying every allocation reason once and stating
  that a refusal outranks a product failure, so the deploy and engineering waits cannot drift apart again.
- Made a server's provisioning state part of admission in `shared/server_admission.py`: `provisioning_phase` must be
  `complete` and an active `PROVISIONING_FAILED` incident refuses the host, fail-closed on unknown phases.
- Kept an unfinished host build an infrastructure situation: the new `AllocationFailureReason.SERVER_NOT_PROVISIONED`
  parks the task in `waiting_resources` and tells the owner via `task_waiting_infrastructure`, not a capacity failure.

## 2026-08-10

- Separated the internal user id from the Telegram chat id across every queue and PO contract: `user_id` is replaced by
  `telegram_chat_id` and `owner_user_id`, PO threads key on the chat, and proactive delivery moved to `proactive.py`.
- Made that separation fail closed: a payload still carrying `user_id` is rejected, `DeployMessage` requires a chat id
  or an explicit `unaddressed_reason`, and the bot's listener claims its previous pending entries instead of auto-acking.
- Restricted live cleanup and write-ahead deployment recovery to API server rows authorized by the managed Time4VPS
  provisioning policy; unrelated rows are skipped, and missing keys or an empty target set stay fail-closed errors.
- Made worker-manager the sole producer of Docker-global identities in generated Compose plans: source-declared volume
  `name` and `container_name` fail closed, and resolved volume names leave the plan so the project name derives them.
- Replaced worker-manager's source-only Compose admission table with one host-capability policy: generated builds admit
  only static workspace-contained `context`/`dockerfile` and `args`, rechecked after resolution and in the snapshot.
- Added a finite Compose v2.27.1 source-directive admission table to worker-manager; `label_file` and unsupported
  loaders are rejected before resolution and the immutable execution snapshot rejects retained loader directives.
- Hardened worker-manager's broker-authenticated Compose boundary: container creation admits only scoped safe
  arguments, so mounts, capabilities, ports, names, environment and identity overrides cannot bypass policy.
- Reworked Compose execution into a runner-owned plan compiler: creation validates source and effective configuration,
  writes a manager-owned snapshot, and executes only that; recovery permits scoped `down -v` without rereading input.
- Fixed effective Compose build validation to resolve relative Dockerfiles from their validated build context, keeping
  safe service-template builds while rejecting a Dockerfile path that escapes the worker workspace.
- Pinned worker Compose source validation and execution to the first selected file's project directory, so multi-file
  paths cannot resolve against another base; build-network selection is unsupported and recovery rejects chosen files.
- Corrected Compose source-tree path contexts: selected files use the fixed project directory while `extends` files use
  their own; recovery now distinguishes global file selection from the supported `logs -f` follow flag.
- Restricted source-only Compose path fields to static literals, rejecting interpolation in `env_file` and
  `extends.file` before project environment values can select a host path during configuration resolution.

## 2026-08-09

- Added executable worker-broker acceptance regressions covering token denial, registration, input
  lease, session/status, typed output acknowledgement and Compose forwarding.
- Coding workers now get only `WORKER_BROKER_URL`, a per-worker token and `WORKER_ID`, and fail
  closed on any direct Redis, API or manager transport value; the Redis session adapter is gone.
- Added the authenticated worker-broker boundary for coding-worker control-plane traffic.
- Coding-agent subprocesses now inherit an explicit allowlist instead of the whole wrapper
  environment, keeping transport URLs and encryption material out while redaction still covers all.
- Worker-manager resolves scaffolded workspace ids against the configured root and accepts only a
  single direct child, so traversal or symlinks cannot escape it on launch or cleanup.
- Coding-worker containers take runtime limits from `WorkerContainerConfig` (4g, 1 CPU, 256 PIDs,
  all capabilities dropped); bind-mount ownership is now fixed from the trusted host side.

## 2026-08-07

- The Claude worker keeps its CLI config in the mounted host session directory: `CLAUDE_CONFIG_DIR`
  now points at the bound path, and `host_session` refuses to start without a writable mount.
- Live-harness clients now pass the global auth gate through three named factories in
  `tests/live/pipeline_helpers.py`, and a missing `INTERNAL_API_KEY` is refused before the first test.
- `docs/DEPLOY.md` now lists exactly the secrets the workflows read, reconciled in both directions
  so an operator configuring from the document passes the required-secret preflight.
- A throttled Time4VPS poll is no longer a verdict: a `401` carrying `wait_x_between_action` is
  waited out and retried, so a succeeding reinstall no longer loses its new root password.
- The live harness records `server_deployment <slug>` before any deploy can start and enriches it
  later, so teardown always holds the stack name; `make test-live-clean` now inventories targets too.
- `handle_provisioning_success` persists the server SSH key before closing the episode, and the
  `provisioning-attempts/reset` endpoint is the single owner of the terminal `READY` status.

## 2026-08-06

- The API answers nobody anonymously: `require_authenticated_caller` fronts every route, accepting
  only an internal key or LK token, with anonymous exceptions listed and reasoned in `ANONYMOUS_ROUTES`.

## 2026-08-04

- Fail-closed Time4VPS provisioning guard: only IDs in `TIME4VPS_MANAGED_SERVER_IDS` are managed,
  records reconcile by provider ID, and a failed SSH probe can no longer select a reinstall.
- The last eight duplicated request schemas have one definition each in the contract; three fields
  became stricter (enums and UUIDs matching their columns) and `RunCreate` was closed by deletion.
- Eleven more request schemas have one definition each, with the server copies deleted and the
  schema modules re-exporting the contract classes, so the API validates what its clients send.
- The dead `LLMNode` layer and its three private modules are deleted, along with the unmounted
  `resources` and `available-models` routers (five API paths) and two unused alias blocks.
- `ProjectCreate`/`ProjectUpdate` have one definition each, so a `github_sync` spec PATCH no longer
  422s; `description`/`modules` are dropped and `status` is typed `ProjectStatus`.
- Every internal API call now carries `X-Internal-Key` and `X-Correlation-ID` from the shared
  transport; the static guard reads `shared/` too and matches on a rule rather than a file list.
- A valid `X-Internal-Key` no longer makes a service anyone's deputy: `resolve_actor` is the single
  place deciding who acts, so a request naming a user is judged as that user.
- `docker/test/service/telegram_bot.yml` gives the bot `INTERNAL_API_KEY` and waits on an import
  healthcheck, so a bot that cannot start is a red suite instead of a green one.
- The internal-API transport is written once in `shared/clients/internal_api.py`; five service
  clients subclass it, dropping 1384 lines and the copies that had drifted on headers.
- `make check-shared-freshness` now answers for the whole tree: every Dockerfile baking `shared`
  must reach a declared image through a build route, and an unreadable route fails by name.
- A built stand can no longer be quietly behind the tree: `make check-shared-freshness` compares the
  baked `SOURCE_HASH` of every reused image with the tree's, with coverage derived from the tree.

## 2026-08-03

- `shared` has one declared form left, the tree: three editable `[tool.uv.sources]` entries and the
  `pyproject.toml` files behind them are gone, since they installed nothing.
- The last floating base image tags are pinned, and `make ci-contract` now fails on any `:latest` or
  untagged image in a Dockerfile or compose file unless it is listed with a reason.
- The CI contract gate derives the test-file list from the tree and fails when a file is run by no
  CI target, so a new test can no longer be invisible; exceptions need a listed reason.
- `services/scaffolder` joins the uv workspace, and its unit tests plus four other directories now
  run on every PR through `make test-unit`; none of the five ran anywhere before.
- `uv.lock` is committed and CI installs from it with `uv sync --locked`; test requirements and every
  base image are pinned, so a CI result describes the tree rather than an upstream index.
- The production deploy resets to the dispatched commit instead of `git pull origin main`, so it can
  no longer ship a newer branch tip than the one validated; a unit test guards `deploy.yml`.

## 2026-08-02

- The production deploy fails before touching the server when critical credentials are absent,
  writes the `.env` at mode `0600`, validates the Compose model, and probes health inside the container.
- Production Compose no longer publishes PostgreSQL, the admin frontend or the user dashboard on host
  ports; operator access goes through the internal network and an SSH tunnel until TLS auth exists.

## 2026-07-28

- `PATCH /api/tasks/{id}` rejects unknown fields instead of dropping them, so sending `status` there
  returns 422 and callers must use the explicit task transition endpoints.
- Offline live-harness contract tests treat `.live-manifests/` as read-only: synthetic manifests go
  under `tmp_path`, and an autouse snapshot guard fails the test that touches a real one.
- Temporary test access to a deployed bot is now a durable state machine on a
  `temporary_access_grants` row, reconciled by a scheduler sweep until the server is observed clear.
- The `telegram_bot` service tests are mounted file by file, since binding the service directory over
  `/app` left an empty `services/telegram_bot/shared` that shadowed the real package.
- Deploy can roll out one named commit: a `head_sha` is pinned by a temporary tag, dropped on every
  outcome, and a run whose finished `head_sha` differs refuses the deploy instead of passing.
- Product analytics are collected again: `LOKI_URL` is a required compose variable and the hourly
  upsert no longer dies on a `UUID` project id, which had left `analytics_hourly` empty.
- The LK tells "no traffic" apart from "nothing was collected": an aggregator heartbeat feeds a
  per-project `collection` block, and the dashboard shows an honest banner when collection is down.
- `.env.example` covers everything compose expects without a default; `LK_DOMAIN` and `LK_JWT_SECRET`
  were missing and rode through as empty strings, and both settings now reject an empty value.
- A failing Time4VPS API no longer hides for a day: response bodies are logged and raised, and a
  repeating failure opens one `provider_api_unavailable` incident, which may omit a server handle.
- `scripts/system_configs.yaml` is the source of truth for the keys it declares: deploy applies it
  after migrations and overwrites diverged values, printing each; other keys stay database-owned.
- ConfigStore separates "the config source is unavailable" from "the key does not exist": an
  unreachable API falls back to the last known value, while a 404 stays a `KeyError`.

## 2026-07-27

- Telegram bot audiences now come from the service-template environment contract: the PO records an
  explicit `bot_access` choice and `TG_BOT_ALLOWED_TELEGRAM_IDS`, and an empty private audience fails early.
- Pinned production scaffolding to service-template `0.3.6`, which makes bot access a contract rather
  than per-project code, after running the Stage 5 compatibility harness against the candidate.
- Grafana now provisions a read-only PostgreSQL datasource plus repository-owned server-capacity and
  run-operations dashboards, so capacity and run history are visible without ad-hoc queries.
- Provisioner playbooks now use the repository Ansible configuration without the removed `yaml`
  callback, restoring role resolution for `deploy_target` and `monitoring`.
- Removed the unused Langfuse receiver stack with its ClickHouse and MinIO dependencies, and the
  OpenTelemetry packages it alone pulled in; the admin Messages view went with the trace data.

## 2026-07-26

### Changed

- Split backend integration coverage into the required Compose-only `backend` suite and the manual
  `backend-dind` suite; worker-container coverage stays in `tests/live/` until host-side gates exist.
- `make test-unit` now points `API_BASE_URL` at an unreachable loopback port, so it cannot read
  configuration from a developer's running API while CI has none.
- Repeated QA failures now leave their fingerprint and attempt evidence on the story held for human
  review, so a reminder cannot restart the same project-level repair loop.
- QA now separates a product failure from an inability to test: a typed `blocked` outcome with
  preflight evidence parks the story for review instead of creating a fix task.

## 2026-07-25

- A stop or undeploy now names its application: `DeployMessage` carries `application_id` and the
  consumer reads the server from it, so a two-server project no longer takes the same container down twice.
- PO can now tear a user's project down via `POST /api/projects/{id}/teardown`, which polls until all
  applications read `not_deployed` before archiving and releasing the bot token.
- Teardown now hands the bot back: archiving a project or an application reaching `not_deployed`
  clears `bot_username` and the token secrets, and `DELETE /api/projects/{id}` clears referencing rows.
- Token validation now refuses a bot another live project already holds, naming the user's own
  project (`bound_to_own_project`) but describing nothing when the holder belongs to someone else.
- Token validation now detects a bot already running on the token outside our system, via
  `getWebhookInfo` and a consume-free `getUpdates` probe whose 409 means another poller holds it.
- Telegram token validation moved server-side behind `POST /api/projects/{id}/telegram/token`, which
  returns a typed verdict and writes secrets only on a pass; other writes now reject token-shaped values.
- The QA runner now sources the server's Telethon credentials into the SSH environment and fails the
  run with a named cause if they are missing, instead of asking the agent to load them.
- Wire Telethon credentials through the QA runner role into `~/.qa-telethon.env` at mode 0600, so QA
  can write to deployed bots as a real user instead of ending every bot check `BLOCKED`.

## 2026-07-24

- Install the QA runner's Claude Code as `deploy_user` rather than root, and run the installer under
  `set -euo pipefail` with a `claude --version` check, ending the `status 127` failures.
- Deploy smoke now verifies the Telegram bot with Bot API `getMe` plus a `docker compose ps` check
  instead of skipping it; Telethon is gone from the deploy worker.
- QA now reads the bot username from the repository record instead of the smoke result, and the PO
  tool raises when it cannot store it, so a working bot no longer fails QA.
- Make missing agent LLM config visible: `.env.example` documents both groups, the architect refuses
  to start without them, and PO logs `po_consumer_disabled` at error.
- Stop project git hooks from gating worker pushes: `setup_git_repo` points `core.hooksPath` at an
  empty directory, since the hook exited 127 in workers and silently dropped real commits.
- Fail engineering when the reported commit is not on origin: the developer node now verifies the
  worker's self-reported SHA against the remote branch instead of only checking it is non-empty.
- Distinguish GitHub's 422 rejections when opening a story PR: "No commits between" now names the
  empty branch instead of being reported as `no existing PR found`.
- Bound ClickHouse log growth with a retention config setting logger level `information`, 200M x 3
  rotation and a TTL on the six `system.*_log` tables.

## 2026-07-20

- Make deploy port selection role-based: allocation and the live harness share port service metadata,
  and deployed URLs come from HTTP-health-serving module ports.
- Fix standalone live-test sweeping after the slug migration: `clean_live_tests.py` selects by
  `title`, carries `slug` through cleanup, and fails the sweep on broken server/key API reads.
- Stop inferring live-work teardown settlement from consumer `status` strings; consumers now mark
  results settled or unsettled so unknown outcomes fail closed.
- Resolve the default-branch head SHA before admin-triggered deploy creation, failing the API request
  before Redis publication when GitHub cannot provide one.
- Fence result-shaped live deploy failures during teardown: failed, cancelled or error-shaped results
  leave the stream entry pending and block cleanup.
- Require an exact `head_sha` before loading a deploy environment contract, returning the typed
  `head_sha_missing` outcome instead of reading from `main`; retries preserve the merged SHA.
- Align local `make lint` with CI by running Ruff format verification before lint, and have
  `make ci-contract` reject future drift between the two.

## 2026-07-19

- Route runtime project identity through immutable `project.slug` in deploy, repository setup,
  allocation, DevOps secrets, smoke logs and QA, with shell-quoted SSH command paths.

## 2026-07-18

- Split project display titles from runtime slugs: projects store free-text `title` plus an immutable
  server-generated `slug`, with API rejection of client-set slugs and a backfill migration.
- Run runnable offline `tests/live/` regressions as one discovered suite in CI and `make test-unit`,
  with `make ci-contract` rejecting a return to per-file enumeration.
- Add OpenAI Codex CLI as a developer-worker type alongside Claude Code and Factory Droid, with a
  pinned `worker-base-codex` image, isolated `HOST_CODEX_HOME` and explicit failure on unknown types.
- Make live harness API reads fail loud before body parsing: parsed `tests/live` responses now call
  `raise_for_status()` first, including scaffold polling and auth-gated checks.
- Route live teardown run discovery through an internal-only API client, so cleanup sees unowned runs,
  cancels active ones and proves terminal status before deleting external resources.
- Extend the live mega pipeline with a real Claude developer worker variant gated on deploy success,
  `/health` 200 and QA passed; real LLM workers get 4GB memory, noop workers keep 2GB.

## 2026-07-16

- Give post-deploy QA one source of truth for acceptance criteria: `Repository.acceptance_criteria` is
  seeded at creation and carried on `QAMessage`, and missing criteria fail the story visibly.
- Fix live server cleanup for compose names that normalize slugs with underscores: teardown discovers
  real compose labels, downs both spellings and fails if residue remains.

## 2026-07-15

- Resolve service-template 0.3.1 PostgreSQL and Redis host ports from persisted allocations via the
  atomic allocation API, filling only missing services on redeploy and failing on ambiguity.
- Persist structured failed GitHub Actions job and step evidence on CI fix tasks, fingerprint repeated
  failures and bound identical fixes before routing the story to human review with one admin alert.
- Made owned-worker teardown idempotent across scheduler and live cleanup races: concurrent removal is
  accepted only after bounded absence verification, and operational failures stay visible.
- Hardened the Stage 7 live mega path: noop engineering pushes its story branch and reports git
  failures, Redis read timeouts are idle polls, and worker cleanup is fail-closed.
- Closed post-merge live cleanup gaps: recover crash manifests first, qualify joined SQL predicates,
  discover workers by ownership label, reconnect idle pubsub listeners.
## 2026-07-14

### Added

- Harden the Stage 7 live harness with verified checkout discovery, per-run ownership manifests,
  fail-closed targeted cleanup and a separate terminal `passed` QA gate after deploy.
- Fence concurrent and reclaimed scaffold executions with atomic per-execution leases during live
  teardown, and route the common live project fixture through the same manifest cleanup contract.
- Add a permanent service-template compatibility matrix for the production pin and explicit release candidates.
- Record requested template source/ref, resolved commit SHA and isolated cleanup outcome as CI artifacts.

### Fixed

- Carry each server's validated `ssh_user` through deploy secrets, precheck, lifecycle actions,
  smoke log collection and QA SSH instead of forcing `root`; provisioning bootstrap remains root-only.
- Pin requested template tags to their resolved commit before scaffolding to prevent tag-move races.
- Resolve candidate commits by fetching the exact revision, including commits that are not advertised branch or tag tips.
- Make template compatibility cleanup fail when Compose teardown or Docker resource verification fails.
- Bound worker compose and incident-journal failures: compose recipes preserve transport, JSON and
  exit failures, and provisioner journal retries publish one terminal result before ACK.

## 2026-07-13

### Changed

- Typed engineering consume: `process_engineering_job` validates input with `EngineeringMessage`
  before any logic, so a malformed job becomes a logged terminal poison entry instead of running on placeholders.

### Removed

- Deleted the dead `langgraph/src/tools/` layer that shadowed the live agent tools and eager-imported
  heavy deps; its only live piece moved to `services/langgraph/src/allocations.py`.
- Removed the `worker:lifecycle` stream and its contract, publisher and vocab slice, since no consumer
  ever existed.
- Deleted the redundant second `agent_config_cache`, a TTL cache stacked on `get_agent_config`, which
  already caches; callers now use it directly.
- Removed the unreferenced legacy `worker-manager/src/scaffold_phase.py` and its unit test, as no
  production caller remained.
- Removed the `shared` compat shims: the swallowed `RedisStreamClient` import is now fail-fast, and
  the legacy deployment aliases, enum and `ensure_consumer_groups` alias are gone.
- Note: raw `publish`/`publish_flat` on `RedisStreamClient` stay public — ~13 production producers
  still call them, and per-consumer migration to `publish_message` continues.

## 2026-07-12

### Changed

- Typed `Run.result`: one `extra="forbid"` model per `RunType` bound to the run's type, so a
  wrong-type, unknown-field or terminal-without-result payload is rejected at the boundary, not left to wedge a story.
- Unified contract vocabularies in `shared/contracts/vocab.py` — one canonical `StrEnum` per
  cross-service concept — replacing competing inline `Literal` sets while keeping per-wire slices distinct.
- Typed response-DTO lifecycle fields: read-side DTOs declare their existing `StrEnum` instead of bare
  `str`, so Pydantic rejects unknown values at the read boundary.

### Fixed

- Worker-mode compose proxy targets: worker-wrapper now overrides service-template's portless
  `worker-start`/`worker-stop` rather than the local-mode `dev-start`/`dev-stop`.
- Pinned production scaffolding: both scaffold paths use the typed GitHub template source and an
  explicit ref, reject floating refs, and record Copier's resolved commit.

## 2026-07-11

### Changed

- Normalize the CI merge gate with a stable `Required CI Gate` job, a mandatory contract check,
  unconditional format/lint/unit runs and assertions that required matrix commands actually ran.

## 2026-07-10

### Fixed

- API service tests after internal auth hardening: compose supplies `INTERNAL_API_KEY` and clients
  send `X-Internal-Key`, and `make test-service` checks container exit codes before cleanup.

## 2026-05-29

### Changed

- Upgrade redis-py to 8.0.0 and make the consumer layer compatible: new decode helpers normalize the
  reads that stopped honouring `decode_responses`, which had silently broken every consumer.
- Moved the QA tester prompt into the `prompts/` package as `prompts/qa/`, matching the architect, PO
  and developer prompts; text preserved verbatim.

## 2026-04-09

### Added

- Stale queue message cleanup: consumers skip and ACK messages for terminal runs or stories, and a
  `queue_cleanup_worker` trims old entries and orphan streams every 10 minutes.

## 2026-03-21

### Added

- Personal-area frontend SPA: new `services/user-dashboard/` React SPA for non-technical founders,
  with token auth, project list, period selector, KPI cards, charts and service status.
- LK API auth and analytics endpoints: JWT flow via `POST /api/lk/auth/token` plus four owner-scoped
  analytics endpoints for projects, summary, chart data and service status.
- Telegram bot `/dashboard` command generates a one-time Redis token and sends an inline URL button
  opening the LK dashboard.

## 2026-03-20

### Added

- Add a `com.codegen.project_id` label to deployed containers so Promtail can discover them and
  expose `project_id` as a Loki label for per-project log filtering.
- Run Promtail on prod servers and expose Loki via Caddy `/loki/*` with Basic Auth, so container logs
  ship to the orchestrator over HTTPS.

## 2026-03-19

### Added

- Regression E2E: `acceptance_criteria` and `bot_username` on Repository, an architect tool to update
  criteria, QA testing full product behaviour, and a QA report view in the admin UI.
- Admin UI action buttons across entity pages: spawn worker, send to architect, stop/undeploy/
  redeploy/run-E2E, plus a secrets editor and story and deploy forms.
- Thin API endpoints for admin actions: eight new endpoints covering story dispatch, worker spawn,
  application lifecycle, E2E runs, deploy-from-repo and secret deletion.
- Queue contracts gain optional `story_id` and an action field: `DeployAction` adds `stop`/`undeploy`,
  and a new `deploy_lifecycle` module handles them over SSH without the full DevOps subgraph.
- Admin UI settings page with System Configs (inline edit per row) and Agent Configs (prompt, model,
  temperature) tabs.
- SystemConfig model, CRUD API and `ConfigStore` client with TTL cache, replacing hardcoded
  operational constants; the scheduler fails fast on missing required configs.

### Changed

- Decouple the QA consumer from story lifecycle: it only writes run status and a `QAOutcome`, while a
  new dispatcher supervisor polls TESTING stories and routes the result.
- Decouple the deploy worker from story lifecycle: it only writes run status and a `DeployOutcome`,
  while `supervise_deploying_stories()` routes success, code fix, retry and give-up.
- Unified worker result API: one `POST /result` replaces `/complete`, `/failed` and `/blocker`, adds
  an `/infra/compose` proxy, captures an stdout tail and auto-resumes agents that exit silently.
- Replaced bare `engineering_status` strings with an `EngineeringStatus` StrEnum and merged the
  blocked and reject handlers, fixing a `blocked` value indistinguishable from a generic crash.

### Fixed

- Restored `_inject_makefile_overrides()` in worker-wrapper, now targeting the compose proxy, so
  `make migrate` and `make dev-start` work inside worker containers again.
- QA consumer resolved the wrong application because the API has no `project_id` list filter;
  `application_id` is now threaded through to a single `GET /applications/{id}`.

## 2026-03-18

### Removed

- Deleted the `orchestrator-cli` package: agents now report results and drive infrastructure via curl
  to localhost:9090, with env vars, CI matrix and docs updated.
- Removed `result_parser` and stdout-based result parsing from worker-wrapper; results flow only over
  HTTP, and the watchdog still auto-fails an agent that exits without reporting.

### Added

- HTTP result server in worker-wrapper on localhost:9090 with three validated POST endpoints, taking
  priority over stdout parsing — the first step in decoupling workers from `shared`.

### Fixed

- Architect `transition_story` now catches 422 and returns the current story state, fixing the race
  where PO had already moved the story to `in_progress`.
- The deploy failure classifier now sees GH Actions failure logs with the failed job and step names
  instead of a bare "failure".
- `create_pull_request` now searches closed and merged PRs on 422, and merged PRs trigger deploy
  directly, breaking the infinite deploy-retry loop.

## 2026-03-17

### Added

- Shared Pydantic DTOs for API entities in `shared/contracts/dto/` (task, story, repository,
  application, incident, plus create/update variants), with incident enums moved to the DTO layer.
- HTTP health prober for deployed applications: probes `/health`, tracks response times, raises
  SERVICE_DOWN and SSL_EXPIRING incidents, auto-resolves on recovery and computes 24h uptime.
- Admin UI application health: new response time, SSL expiry and uptime fields plus a 7-day history
  table, health column and expandable response-time chart.
- Admin UI extended server health dashboard: tabbed expandable rows with overview cards, per-container
  metrics, CPU/RAM/disk charts and incident history.
- Health checker worker polling node_exporter and cadvisor for managed servers, updating server health
  fields, raising and auto-resolving incidents with dedup, and notifying admins.
- Prometheus text format parser for node_exporter and cadvisor `/metrics`, with node and container
  metric extraction over the full exposition format.
- Server health metrics model and history table: nine health columns on Server plus a 7-day retention
  `server_metrics_history` table and its API endpoints.
- Provisioning adds cadvisor alongside node_exporter, restricts ports 9100/8080 to the orchestrator IP
  via UFW, and includes the monitoring role in `provision_software.yml`.

### Changed

- Migrated every service API client from raw dicts to the typed DTOs, replacing dict-key access with
  attribute access across scheduler, LangGraph, scaffolder and infra-service.
- Refactored ten files over 400 LOC by extracting helper modules, with every original module
  re-exporting for backward compatibility.

### Fixed

- Contract violations from an audit: hardcoded status strings and queue names replaced with shared
  enums and constants, and a defaulted `API_URL` lookup made fail-fast.
- cadvisor parser filtered out cgroup v2 Docker containers because their ids start with
  `/system.slice`; entries containing `/docker-` are now allowed, restoring the admin Containers tab.

## 2026-03-16

### Fixed

- Deploy failure classification and worker rejection: fixed the classifier model id, replaced binary
  CODE/INFRA with CODE_FIX/RETRY/GIVE_UP defaulting to retry, and wired up the dead reject path.
- `service_deployments.updated_at` had no server default, so every INSERT raised
  `NotNullViolationError` and deploy records failed silently; a migration adds `server_default=now()`.

### Removed

- Removed the admin Prompts tab, its `/prompts` endpoints and Redis persistence of `task_md` and
  prompt history, since the `-p` argument is now a hardcoded constant.

### Changed

- Deploy to QA handoff: the deploy consumer transitions the story to `TESTING` and publishes a
  `QAMessage` instead of completing it; standalone webhook deploys bypass QA.

### Added

- New `qa_runner` Ansible role provisions prod servers for QA: Claude Code CLI, telethon and httpx in
  a venv, a 2GB swap file to prevent OOM, and credential-file auth.
- QA consumer skeleton reading `qa:queue`, running Claude Code over SSH with a story-based prompt and
  parsing the result; pass completes the story, fail creates a fix task, capped at 2 loops.
- `StoryStatus.TESTING` plus the QA queue contract: new transitions, a `POST /api/stories/{id}/test`
  endpoint, the `QAMessage` contract and queue constants.
- PR merge polling: the dispatcher polls GitHub for merged PRs on `pr_review` stories every 30s,
  removing the dependency on the webhook for the transition to deploying.
- Deploy failure LLM classifier splits CODE from INFRA before dispatching engineering, so infra
  failures retry the deploy instead of wasting a worker.

## 2026-03-15

### Added

- Branch protection after scaffold: GitHub `main` now requires a PR and a passing `ci` check, set
  non-fatally so scaffold still succeeds if the call fails.
- Feature branches for stories: workers operate on `story/{story_id}`, with the branch name flowing
  through the full pipeline and the wrapper pulling from it instead of `main`.
- PR-based CI gate replaces the polling gate: the dispatcher opens a PR from the story branch and
  enables auto-merge, with webhooks handling merges and CI failures.
- Task archiving in `.story/old_tasks/`: the wrapper merges TASK.md and REPORT.md per task so the next
  worker can browse prior tasks instead of being force-fed story context.
- Hybrid `--resume` session management: a `clear_session` flag forces a fresh Claude CLI session on
  retries, so a retry does not inherit errors from the failed attempt.

### Changed

- TASK.md moved to `/workspace/TASK.md`, and the wrapper archives TASK.md plus REPORT.md into
  `.story/old_tasks/{task_id}.md` so the next worker sees full history.
- Claude workers now get a one-line `-p` prompt pointing at TASK.md instead of the full task content,
  keeping the context window clean.
- Merged AUDIT_REPORT.md into REPORT.md: workers already write Issues and Suggestions there, so the
  separate audit report was redundant.
- Filter scaffolder tree output to exclude `.venv`, `node_modules`, `.git` and cache directories,
  saving tokens in the architect's context.
- The E2E skill now saves worker reports to local files before DB cleanup, so reports are not lost
  when task events are deleted.

## 2026-03-14

### Changed

- Bind `PortAllocation` to Application via `application_id` instead of Project; applications now carry
  a one-to-many ports relationship and are created at allocation time.
- Unified workspace management around `repo_id`: the scaffolder is the sole source of truth at
  `/data/workspaces/{repo_id}/`, and legacy project-id paths and config were removed.

### Added

- Introduced `Application` as a first-class runtime entity separate from the immutable `Deployment`
  log, with its own status enum, CRUD API and a backfill migration.
- Tasks page gains multi-select status and type filters and sortable Status, Priority and Updated
  columns, plus a new `MultiSelect` component.

## 2026-03-13

### Added

- Ensure-workspace gate: scaffolding always runs before the pipeline proceeds, with a new `ensure`
  mode that skips, clones or errors, fixing crashes when a workspace was garbage-collected.
- Workspace browser: workspace as a first-class entity with tree and file endpoints in worker-manager
  and shared React components used by both the project and worker pages.
- Admin SPA gains LLM Tracing and Users pages, with a Langfuse iframe, user detail pages and an
  `owner_id` filter on the projects API.
- LangChain to Langfuse tracing integration: a `tracing.py` utility supplies a callback handler when
  the keys are set, wired into all four consumers with no agent or graph changes.
- Self-hosted Langfuse v3 infrastructure: langfuse-web, langfuse-worker, ClickHouse and MinIO in
  compose, a separate Langfuse database and an nginx proxy at `/langfuse/`.
- Queue message browser: new debug endpoints for stream messages and pending entries, plus admin queue
  detail pages with parsed previews, delete, ack and consumer info.
- New `WorkerStatus` StrEnum replaces hardcoded worker status strings across worker-manager and
  langgraph.
- Admin phase 2: worker inspector with live console, prompts and file tabs, a kill button, typed
  queue pages, and task retry and resume buttons.
- Worker-manager introspection API: seven endpoints for listing workers, detail, container logs,
  workspace tree, file content with traversal protection, prompts and kill.
- Admin auth and a single entry point: nginx basic auth on the frontend, Grafana proxied under
  `/grafana/`, and external Grafana and API ports closed.
- Admin frontend scaffold: React 19 plus Vite and Tailwind SPA with sidebar layout, live dashboard,
  project and task lists with filters and detail pages, served by nginx.
- Observability stack: Loki, Promtail and Grafana in compose plus correlation-id binding from Redis
  messages and `X-Correlation-ID` propagation across all API clients.
- Architect specs context: the scaffolder parses YAML spec files into a compact `specs_summary` on the
  project config, so the architect sees models, operations and events when decomposing.
- Architect scaffold wait: the consumer polls `project.status` and waits up to 5 minutes for scaffold
  completion instead of decomposing without tree or spec context.
- Parameterized `get_project_spec`: the architect can request a compact summary by default or full
  model, event or domain definitions, saving tokens until a deep dive is needed.
- PO `get_story` now returns each task's runs, so PO can answer "how's it going?" without a separate
  `get_run_status` call.
- PO now accepts the `story_blocked` system event, previously dropped, and the prompt answers calmly
  that a specialist is reviewing.
- Runs API accepts a `task_id` query filter, and `RunRead` includes `task_id`.

### Fixed

- Admin detail pages persist the active tab in URL search params, so a refresh no longer loses it;
  the workspace tree also auto-refreshes.
- Audit cleanup: use status enums, proper exception chaining, `HTTPStatus` constants in telegram
  handlers, and fail fast on a missing `API_BASE_URL`.
- Worker lifecycle cleanup: `delete_worker()` now removes the worker's input/output streams, orphan GC
  does a reverse Redis-to-Docker check, and workspace GC scans both base paths.
- Architect story spam: the consumer transitions a story to `IN_PROGRESS` on pickup and skips already
  decomposed stories, and the retry counter moved to Redis so it survives restarts.

### Changed

- Architect prompt rewritten away from scaffold-centric framing toward an existing service with specs,
  adding decomposition philosophy about slicing into iterations.
- Developer blocker guidance rewritten in INSTRUCTIONS.md: try to solve first, but never ship code
  that compromises product quality.

## 2026-03-12

### Added

- HITL MVP: a `WAITING_HUMAN_REVIEW` status for tasks and stories, a blocker-report path from the
  worker, admin and user notifications, and a `POST /tasks/{id}/resume` endpoint with guidance.
- Story and task reopen flow with `user_report`: PO can reopen a completed story instead of creating a
  new one, and the architect and developer both see the reopen context.
- PO `validate_telegram_token` tool validates bot tokens via `getMe` on receipt and stores the token
  and username, so an invalid token fails at PO instead of after 30 minutes of pipeline.
- Smoke failures now include container crash logs: `SmokeTesterNode` captures `docker compose logs`
  over SSH so fix tasks receive real tracebacks instead of a bare HTTP 500.

### Changed

- Split the 13-value `ProjectStatus` enum into `ProjectStatus` (lifecycle), `ServiceStatus` (runtime)
  and `RepositoryStatus`, with a data migration; consumers now touch only `service_status`.

### Fixed

- `_check_project_lock()` now cleans Redis keys for workers in terminal states, unblocking task
  dispatch without manual cleanup.
- Deploy retry limit of 3: consecutive attempts per story are tracked in Redis and the story fails
  afterwards, ending the infinite deploy-fail-redispatch loop.
- Deploy deduplication now uses an atomic Redis `SET NX` lock per project instead of a non-atomic DB
  check, closing the race that triggered duplicate deploy workflows.

## 2026-03-11

### Added

- Deploy to engineering feedback loop: a failed smoke test or workflow re-dispatches a fix task,
  capped at 2 attempts via a `deploy_fix_attempt` counter on both message contracts.
- PO proactive secret collection: PO identifies required paid API keys from the project description
  and asks the user before engineering starts.
- New `DEPLOYING` story status gates completion on a successful deploy: `complete_stories` transitions
  and triggers the deploy with the right action, rolling back to `in_progress` on failure.

### Fixed

- Proactive message spam filter: only deploy success and permanent story failure reach the user, so
  deploy, smoke and precheck noise no longer floods `po:proactive`.
- Deploy auto-fallback from `create` to `feature` when the precheck reports the directory already
  exists, removing the most common manual intervention.
- CI-check tasks that find nothing to fix no longer fail on "no commit was made": an `allow_no_commit`
  flag lets the developer node report done and skip the commit and CI gates.
- Deploy action was always `create` for already-deployed projects; `complete_stories` now checks the
  project status and sends `feature`, preventing precheck failures on updates.
- Replaced roughly 30 hardcoded status string literals with the shared `TaskStatus`, `StoryStatus` and
  `ProjectStatus` enums across four services, so renaming a value cannot break silently.
- Removed four locally defined Redis queue name constants that duplicated `shared/queues.py`, and
  replaced direct `xadd()` calls with the stream client where available.

### Changed

- CI gate now runs once per story instead of per task: ordinary tasks commit without pushing and the
  system CI-check task pushes and runs the gate, saving GitHub Actions minutes.

## 2026-03-10

### Added

- Live pipeline test suite in three tiers (scaffold, engineering, full deploy) with shared helpers,
  Makefile targets, always-on cleanup and a debug dump on failure.
- Smart CI failure triage: workers signal `## REJECTED` for infrastructure failures, which stops
  retries immediately, fails the task and story with reject metadata and notifies admins.
- E2E Pipeline V2 smoke test: first full PO to worker run, confirming the PO to architect flow and
  surfacing three blocking bugs (all fixed) plus one medium.

### Fixed

- `ProjectStatus` lacked `ERROR`, so the value the deployer wrote crashed the scheduler's project
  fetch in a loop and tasks stuck at todo; the enum member was added.
- Scaffolder tried to clone a GitHub repo that did not exist; it now creates the repo first,
  idempotently.
- Scaffolder now updates `git_url` to the real GitHub URL after creating the repo, instead of leaving
  the `pending://` placeholder the CI gate could not resolve.
- `github_sync` passed a UUID object to `json.dumps` and raised `TypeError`; the project id is now
  stringified.
- `TaskCreate` omitted `status`, so architect tasks silently defaulted to backlog; the schema and
  router now accept it.
- PO `create_project` created a project and story but no repository, which `scaffold_trigger`
  requires; it now creates one with a placeholder `git_url`.
- The `scaffolder` service was defined in compose but never built or started; it is now running.

### Changed

- Architect prompt now prefers fewer, larger tasks — one task per story is fine for simple projects,
  split only for genuinely different concerns.
- Makefile `stop` is now an alias for `down`, which kills worker containers and cleans the network.
- The scaffolder now receives `GITHUB_ORG` in compose, and a PEM mount path typo was fixed.

## 2026-03-09

### Added

- New `services/scaffolder` microservice consumes `scaffold:queue` and prepares project repositories
  before the architect runs, so decomposition can see the project tree.
- Worker reuse per story: one container is spawned per story and reused for later tasks via a
  `story:workers` Redis hash, saving about 50s per task.
- Pipeline failure supervisor: three functions in the 30s dispatch loop retry stuck stories, reopen or
  fail failed tasks with sibling cancellation, and time out `in_dev` tasks.
- PO tools contract tests validating tool payloads against the API's Pydantic schemas, plus
  integration tests running the full tool-to-DB roundtrip as a new CI suite.
- Architect node decomposes a story into chained tasks and a task dispatcher polls every 30s to
  dispatch unblocked ones with sibling context, completing the story when all tasks are done.

### Changed

- Worker-manager mounts pre-scaffolded workspaces by `repo_id` instead of running copier inside
  containers, and the engineering consumer passes story context so workers keep continuity.
- Architect prompt is now scaffolded-aware: it creates tasks only for the business-logic diff, sees
  the project tree, and a CI check task is appended automatically.
- Update Ruff to 0.15.5 in pyproject and CI, reformatting 17 test files; no functional changes.
- Removed the Docker tooling images and pre-commit config in favour of `uv run` everywhere, with the
  ruff version pinned only in `pyproject.toml` and `uv.lock`.

### Refactored

- Migrated the architect from a scheduler function to a LangGraph ReAct agent in its own Docker
  service, with five tools and a custom consumer group.
- LangGraph directory refactoring: `src/workers/` became `src/consumers/`, `src/worker/` dissolved
  into the root and PO prompts centralized — structure only, no logic change.

## 2026-03-08

### Fixed

- **Deploy: inter-service URL uses docker service name** (#54): inter-service vars now resolve to docker DNS
  (`http://backend:8000`) via `API_URL` in `COMPUTED_EXACT`; external-facing URLs unchanged.
- **compose.dev.yml ports conflict with worker containers** (task-f9aadfc1): the compose runner clears published ports for
  worker projects, which talk over Docker DNS and clashed with the orchestrator's own ports.
- **Missing Project warnings spam**: `github_sync` now respects `GITHUB_ORG` instead of picking the first installed org,
  which raised false `MISSING` alerts in multi-org installs.
- **Admin notifications spam**: `notify_admins` filters on the `is_admin` flag instead of messaging every user.
- **Test Infrastructure Audit**: fixed 10 bugs and warnings — parallel `make test-unit` (35s → ~12s), unmocked `notify_admins`,
  a missing test header, deprecated 422 constants, and asyncpg engine leaks.
- **Scaffold script task_description escaping** (#52): pass the description through a base64-encoded copier `--data-file`
  instead of inline `--data`, closing shell metacharacter injection.

### Refactored

- **Split engineering_worker.py** (#18): extracted CI gate into `_ci_gate.py` and repo setup into `_repo_setup.py`,
  halving the main file. Internal only, no behavior change.

### Changed

- **Decouple shared/ from Docker builds** (task-7e9aed9c): replaced `pip install shared` with `COPY shared` + `PYTHONPATH`
  and narrowed `WORKER_SOURCE_HASH`, cutting rebuilds after a shared/ change to ~10s.
- **Dockerfile layer caching optimization** (#21 deviation): split shared installs into deps-then-code steps and made the
  Claude CLI install multi-stage, so base image changes no longer bust the cache.
- **Replace Milestone with Story type field** (task-6fe23f2a): added a product/technical `type` to Story and deleted the whole
  Milestone stack, so phases are expressed by story type instead of a second entity.
- **Project ID → UUID + schema cleanup** (task-7163e7ac): `Project.id` and its 13 FKs move to native `Uuid`, legacy repo
  fields give way to `Repository.provider_repo_id`, and the migration converts mixed-format ids.

### Added

- **Deploy Pre-Check** (#21): `DeployMessage` gains an `action` field and the deploy worker SSH-checks the target dir first,
  so `create` fails on leftovers and `feature`/`fix` fail when never deployed.
- **Seed DB — stories, repositories, historical tasks** (task-f7cd9611): seeded repositories and stories, linked all
  orchestrator tasks, imported the service-template backlog, and taught `/triage` story matching.
- **TaskStatus.BLOCKED + blocked_by_task_id** [hotfix]: new `blocked` status with a self-referencing FK and transitions to/from
  `in_dev`; `/implement` auto-unblocks a task once its blocker is done.
- **Story: priority + blocked_by fields** (task-9d288940): Story gains `priority` and `blocked_by_story_id` with list filtering
  and sorting; starting a story whose blocker is unfinished now returns 422.
- **Story model + API** (wi-34761901): new `Story` entity with a status state machine, full CRUD plus `/start|/complete|/archive`,
  a nullable `Task.story_id` and self-referencing parent for epic-like grouping.
- **Repository model + migration** (wi-ad3b4502): new `Repository` entity with CRUD plus `by-provider-id` lookup, a
  `RepositoryRole` enum and a nullable `Task.repository_id`.
- **make sync — docs generation from DB** (task-94f2783f): a `tasks/push` endpoint and generator scripts feed `make sync`,
  so STATUS.md, backlog and recent artifacts render from the DB instead of stale files.
- **PR flow + in_ci status + need_e2e** (#64): renamed `IN_REVIEW` to `IN_CI` and added `need_e2e`, so `/complete` promotes
  in_dev → in_ci → testing → done and `/implement` drives push → PR → CI → merge.

## 2026-03-07

### Changed

- **Rename WorkItem→Task, Task→Run** (#64): the planning entity becomes `Task` and the execution one `Run`, with tables,
  ID prefixes, API routes and a migration renaming everything in order.

### Added

- **Milestone model + ROADMAP generation** (#63): milestones become DB entities with CRUD, a `complete` action and
  `WorkItem.milestone_id`, so ROADMAP phases are generated rather than hand-written.
- **Brainstorm model in DB** (#61): brainstorms become first-class entities with a draft→archived state machine, CRUD and action
  endpoints, and `WorkItem.source_brainstorm_id` linking work back to its origin.
- **Skills → API + Simplified Model** (#58): all skills read and write the Work Items API instead of markdown, via a `plan`
  field, `COMMENT` events, new list endpoints and `make backlog`.
- **`/implement` emits work item events** (#57): the skill writes `step_start`/`step_done` events per plan step and calls
  `/complete` at the end, giving each task an event trail.
- **`/next` skill via Work Items API** (#56): first skill off markdown parsing — picks and starts tasks over the API, with
  `limit`/`sort` params and a `by-tag/{tag}` lookup.
- **WorkItem task management system** (#55): planning layer with `WorkItem`/`WorkItemEvent`, agile statuses, an action-based
  API with state machine validation, and a script migrating backlog.md into the DB.

### Fixed

- **Secrets not persisting**: the plain `JSON` column missed in-place mutations, so the secrets endpoint dropped writes;
  fixed with `MutableDict.as_mutable(JSON)` and a dict copy in `merge_secrets` (#51)
- **Project stuck in "deploying"**: deploy-worker now rolls the project back to `failed` on `missing_user_secrets` (#51)
- **API service tests event_loop**: replaced the deprecated `event_loop` fixture with a session loop scope to fix
  "Future attached to a different loop" errors (#51)
- Description loss in create flow: `trigger_engineering` now PATCHes `detailed_spec` into project config for `action=create` (#50)
- `_build_create_task` uses `feature_description` from queue as fallback when `detailed_spec` is missing (#50)
- PO prompt updated to pass description to both `create_project` and `trigger_engineering` (#50)

## 2026-03-06

### Added

- Telegram admin "Add User" button: inline keyboard + text input flow to create users via `POST /users/` (#49)
- `POST /api/projects/{id}/config/secrets` atomic merge endpoint with `SELECT FOR UPDATE` locking (#47)
- `merge_secrets()` method on `LanggraphAPIClient` (#47)
- Concurrent secrets merge integration test (#47)
- `user_name` field on `POUserMessage`; telegram bot populates from `tg_user.first_name` (#45)
- User context injection `[context: user_id=..., user_name=...]` prefix on PO messages (#45)
- `hint` parameter on `set_project_secret` tool; hints stored in `config.env_hints` (#45)
- PO prompt sections: env hints usage, access control question for tg_bot projects (#45)
- `_format_env_hints()` in DeveloperNode — injects `## Provided Environment Variables` into TASK.md (#45)
- Integration test: verify env_hints appear in worker TASK.md (`test_task_injection.py`) (#45)
- PO `web_search` tool: DuckDuckGo search for third-party API documentation (#44)
- System prompt guidance for when to use web search vs. existing knowledge (#44)
- PO Socratic dialog: requirements gathering before triggering engineering (#43)
- PO prompt focuses on product questions for non-technical users, avoids technical details (#43)
- `trigger_engineering` docstring emphasizes passing full gathered spec as description (#43)
- Unit tests for PO prompt content and tool docstrings (#43)
- Workspace failure counter: tracks consecutive failures per project in Redis (#8)
- Force workspace wipe after 2 consecutive failures — broken state auto-recovery (#8)
- Spawn rejection after 3 consecutive failures — circuit breaker with auto-unblock (TTL 48h) (#8)
- `reason` field on `DeleteWorkerCommand` — `completed`/`failed`/`timeout` for failure tracking (#8)
- `--feature` mode in e2e-run skill: triggers `action=feature` after create+deploy and monitors feature CI+deploy (#34)
- Feature Add Matrix in e2e-run skill: per-test feature descriptions for all 7 test cases (#34)
- Unit tests for `action=feature/fix` flow in DeveloperNode and engineering worker (#34)
- `GET /projects/by-repo-id/{repo_id}` — lookup project by GitHub repo ID, used by scheduler github_sync (#33)
- `GET /servers/{handle}/ssh-key` — returns decrypted SSH private key per server (#33)
- `PATCH /servers/{handle}` accepts `ssh_key` field — encrypts with Fernet and stores (#33)
- Provisioner auto-saves SSH key to DB after successful provisioning (#33)
- `LanggraphAPIClient.get_server_ssh_key()` — fetches per-server SSH key (#33)
- `_ssh_key_tempfile()` context manager for secure temporary SSH key files (#33)
- `docker-compose.prod.yml` — production overlay (no direct API port, restart policies, Redis AOF, no DB defaults) (#32)
- `infra/scripts/pull-worker-images.sh` — pulls worker base images from GHCR and retags to local names (#32)
- `infra/scripts/backup-db.sh` + systemd timer — daily pg_dump with 7-day rotation (#32)
- `docs/DEPLOY.md` — full production deployment guide with GitHub Secrets inventory (#32)

### Fixed

- Corrupted checkpoint recovery: PO consumer auto-repairs orphan tool_calls that block users permanently (#48)
- `ruff.toml` per-file-ignores now covers `**/tests/**` paths (services tests were getting PLR2004 false positives) (#48)
- Race condition in `set_project_secret` when LLM calls it in parallel — secrets no longer lost (#47)
- `test_post_projects_pure_db` integration test — add `X-Telegram-ID` header and seed user via API (#42)
- CI: service test matrix `changed` field was a literal string, not a `${{ }}` expression — tests were silently skipped since #4 (#38)
- API: make `X-Telegram-ID` optional for project creation — system calls create discovered projects with `owner_id=None` (#38)
- Service test `test_pure_crud`: removed unnecessary `X-Telegram-ID` header (test verifies no side effects, not ownership) (#38)
- Service test `test_service_db_smoke`: fixed event loop mismatch caused by session-scoped DB engine (#38)

### Changed

- `set_project_secret` PO tool uses single POST instead of GET→decrypt→merge→encrypt→PATCH (#47)
- `_save_secrets_to_project` in devops nodes delegates to `api_client.merge_secrets` (#47)
- `owner_id` on projects is now NOT NULL — every project must have an owner (#39)
- `POST /api/projects/` returns 400 if `X-Telegram-ID` header is missing (#39)
- `github_sync` no longer creates orphan projects — sends admin notification for unknown repos (#39)
- Webhook removes `if project.owner_id` guard — owner always exists (#39)
- `ProjectDTO.owner_id` is now `int` (was `int | None`), `ProjectRead` includes `owner_id` (#39)
- DeployerNode reads SSH key from DB (per-server) instead of mounted file (#33)
- `run_ssh_command()` accepts `ssh_key` content parameter instead of reading from `Paths.SSH_KEY` (#33)
- `docker-compose.yml`: parameterized `SSH_KEY_PATH` and `GITHUB_APP_PEM_PATH` with dev defaults (#32)
- `.github/workflows/deploy.yml` rewritten: writes env vars, builds images, pulls worker images, migrates, health-checks (#32)

### Removed

- `ProjectUpdate.owner_id` field — owner is immutable after creation (#39)
- `SchedulerAPIClient.create_project()` — scheduler no longer creates projects (#39)
- SSH volume mounts (`~/.ssh:/root/.ssh:ro`) from langgraph, deploy-worker, scheduler, infra-service (#33)
- `Paths.SSH_KEY` from `shared/constants.py` — no longer needed (#33)
- `ORCHESTRATOR_SSH_KEY` secret from deploy.yml — per-server keys in DB now (#33)

## 2026-03-05

### Fixed

- Atomic port allocation: `UniqueConstraint(server_handle, port)` + `POST /ports/allocate-next` with `SELECT FOR UPDATE`
  eliminates the TOCTOU race in parallel deploys (#31)
- Multi-user isolation: PO tools now pass `X-Telegram-ID` header in all API calls (#30)
- API requires `X-Telegram-ID` for project creation — prevents orphan projects with `owner_id=NULL` (#30)
- Workers pass user's telegram_id to API when fetching projects, enabling ownership checks (#30)
- `LanggraphAPIClient.get_project()` and `list_projects()` accept optional `telegram_id` param (#30)
- Fail fast with `RuntimeError` when `ORCHESTRATOR_USER_ID` is unset in CLI commands (#29) — it silently defaulted to
  `"unknown"`, breaking the audit trail

### Removed

- Dead `ports.py` PO tools (`allocate_port`, `get_next_available_port`) and `PortAllocationResult` schema — replaced by
  atomic allocation in `allocator.py` (#31)
- Dead `list_repos.py` debug script from langgraph service (#17)
- Legacy name-based project lookup fallback in github_sync, including `get_project_by_name` on the scheduler client (#17)
- Dead CLI agent config infrastructure (#36): `CLIAgentNode`, its cache, API router/schema/model and migration
- Dead `architect_complete` field from `OrchestratorState` and provisioner init (#37)
- Vestigial references to removed agents (architect, Zavhoz, product_owner, brainstorm, developer) in comments/docstrings (#37)

### Changed

- Replaced last "Zavhoz agent" reference with "ResourceAllocatorNode" in `AllocatedResource` docstring (#12)
- Documented engineering-worker and deploy-worker as Redis stream consumers of the langgraph image, not standalone services (#12)
- CI integration tests: sequential → 5 parallel matrix jobs (backend, cli, template, frontend, infra) (#4)
- Per-suite change detection: each integration suite only runs when relevant files changed (#4)
- Healthcheck intervals 5s→2s in non-DIND test compose files (frontend, infra, cli) (#4)
- Per-suite Docker buildx cache keys for better cache hits (#4)
- Defensive init `smoke_result: None` in `_build_subgraph_input` (#25) — consistent with other Optional fields
- Diagnostic logging `devops_subgraph_result` in deploy_worker after `ainvoke()` — for #25 root cause investigation
- Updated `/e2e-run` skill to check deploy-worker logs for smoke diagnostics
- Extract `infra_client.py` (279 LOC) from langgraph + infra-service to `shared/clients/` (#23)
- Merge duplicated constants (`Paths`, `Timeouts`, `CI`, `Provisioning`) into `shared/constants.py` (#23)
- Service-local `config/constants.py` now re-exports from shared (#23)
- Add `shared/tests/**` to ruff PLR2004/S101 per-file-ignores (#23)
- Restructure ROADMAP: split Phase 2 → 2A (pre-MVP alpha blockers) + 2B (post-alpha stability)
- Triage: 7 new tasks (#30-#35), reopened #25 as regression, reordered backlog by roadmap phases
- New brainstorm: epic decomposition — decision: Task Store in DB (Phase 3), skip intermediate file-based epics
- Triage skill: added Queue reorder step based on ROADMAP phase priorities

### Added

- E2E report: todo_api with-PO mode PASS (12 min) — first test with PO creating project via Redis Streams
- Post-deploy smoke tester node in DevOps subgraph (#25): HTTP `/health` check for backends, Telethon `/start` for tg_bot
- `SmokeTesterNode` with retry logic (3 retries, 5s delay) and graceful skip when Telethon not configured
- `smoke_result` field in `DevOpsState` — propagated through deploy_worker to task result
- Conditional routing: `deployer` → `smoke_tester` → END (skips smoke on deploy failure)
- Telethon dependency + env vars in deploy-worker compose config
- Updated `/e2e-run` skill to report smoke results

## 2026-03-04

### Added

- Auto-detect stale worker images via a source hash label, `check-worker-images` and auto-rebuild in `make build` and E2E
  pre-flight, after a `shared/` fix went unbaked into the worker image for 4 E2E runs
- LangGraph integration tests (#6): 3 tests against real DB/Redis/API (engineering worker flow, missing project, scaffold_failed abort)
- Engineering-worker service in backend test compose
- API data seeding fixtures (`seed_project`, `seed_task`, `seed_server`) + `poll_task_status` helper
- E2E reports: todo_api Level C PASS (14 min), weather_bot Level C PASS (15 min, first multi-module test)

### Fixed

- Enforce fail-fast for env vars (#24): notifications.py uses lazy init — import stays safe, first call raises if
  `TELEGRAM_BOT_TOKEN`/`API_BASE_URL` are missing
- Replace `print()` with `logging.warning()` in tool_registry.py (#24)
- Replace swallowed `except: pass` with `logger.debug()` in worker-manager events.py (#24)
- Add ORCHESTRATOR_USER_ID warning in CLI commands (#24)
- Alembic migrations in test API + encryption key for integration tests (#6)
- Missing `__init__.py` for relative imports in integration tests (#6)

### Changed

- Consolidated duplicated test helpers (`wait_for_stream_message`, `wait_for_create_response`) into `conftest.py`

## 2026-03-03

### Fixed

- CI gate: filter by commit SHA to prevent scaffold CI satisfying implementation gate
- BACKEND_PORT: resolve from allocated resources instead of random secret token

### Added

- Worker network isolation (#22): `codegen_worker` network, dual-homing bridge services
- E2E report: todo_api Level C — full pass, all CRUD working (14 min end-to-end)

### Changed

- Remove obsolete EXEC_MODE=native references

### Removed

- `project-db` alias workaround and `_patch_db_hostname()` (#22) — no longer needed with network isolation

## 2026-03-02

### Fixed

- Docker network overlap in compose volume test
- Phantom TaskType re-export in shared.models (multiple attempts)
- CI unit test targets — use unified `make test-unit` with uv

### Changed

- Consolidate test suites: clean up Makefile targets, fix worker-manager tests (#6)
- Move enums to contracts/dto (single source of truth)
- Cleanup migrated service tests
- Add service tests to CI

### Added

- E2E reports: todo_api Level C — deploy failed, makemigrations investigated
- Backlog #6 audit: service test details

## 2026-02-28

### Fixed

- Compose proxy: file discovery, env leak, DNS collision
- Use `infra/` compose layout in ComposeRunner

### Added

- E2E secret injection for tg_bot Level C tests
- Deploy retry: rerun failed workflow

### Changed

- Backlog #6 audit: service & e2e test broken items documented

## 2026-02-27

### Fixed

- Compose workspace path mismatch for project-id workers
- Docker login resilience + infra failure rerun in CI
- Dead worker container detection — unblock waiting consumers

### Added

- E2E Level C run reports (multiple iterations)
- E2E skill pre-flight checks

## 2026-02-26

### Fixed

- Scaffold skip due to stale project dict + 3-level fail-fast defense
- Fail-fast when GitHub repo already exists during engineering job
- WorkflowNotFoundError fail-fast and description fallback in CI gate

### Changed

- Use GitHubAppClient instead of gh CLI in e2e-run skill

## 2026-02-25

### Added

- Encrypt API keys and SSH keys at rest using Fernet (#20)
- Refactor audit v2 report

### Changed

- Unify ServerStatus enum, remove dead IncidentDTO (#15)
- Consolidate ServiceModule enum, remove dead code (#16, #17)
- Sync worker prompts with simplified service-template (#1)
- Migrate `(str, Enum)` to `StrEnum` across codebase (21 instances, 14 files)
- Remove deprecated `update_framework` command
- Remove stale ruff.toml per-file-ignores
- Deduplicate MockProcess into shared test conftest

## 2026-02-23

### Fixed

- Pin fakeredis>=2.34.1 to eliminate deprecation warnings
- Timezone=True for Task model datetime columns
- Healthcheck intervals tuned, worker-manager lock refresh
- Scaffold skip bug, description passthrough, CI gate 404 handling

### Added

- E2E testing skills for Line 2 engineering pipeline (e2e-run, e2e-check, e2e-cleanup)

## 2026-02-21

### Changed

- Remove stale scaffolder references
- Add audit report collection step to Line 2 playbook

### Added

- DELETE /api/projects endpoint
- Line 2 engineering playbook

### Fixed

- Remove "backend always required" constraint from module selection

## 2026-02-20

### Added

- Scaffolder removal: inline scaffold phase into worker-manager (#1 orchestrator-side)
- Orphaned resource GC for worker-manager

### Changed

- Extend e2e scaffold test with verification and cleanup
- Remove Docker-in-Docker capability, update developer prompts (dev-env phase 4)
- Add native tooling packages to worker-base-common image

## 2026-02-19

### Added

- Workspace persistence: project_id passthrough, git token refresh, PROGRESS.md, GC by age, project mutex (phases 1-5)
- Worker reuse for CI fix loop: wrapper multi-turn, spawner API, engineering reuse, gate timeout (#8)
- Dev environment: workspace bind-mount, dual-network, compose proxy (phases 1-3)

## 2026-02-17

### Added

- Redis Streams unification: 9 consumers on `RedisStreamClient.consume()` with PEL recovery, Pydantic contracts (#3+#5)

## 2026-02-15

### Added

- Deploy architecture (9 iterations): Fernet encryption, env groups, GitHub Actions deploy, webhook auto-deploy,
  self-hosted Docker registry + Caddy TLS
- PO ReactAgent migration: CLI subprocess → async LLM consumer with reminder polling and direct tool access
