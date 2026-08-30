# Changelog

## Unreleased

### Changed

- Removed historical planning and incident documents, reconciled living documentation with current
  pipeline, QA, secret, worker, and lifecycle behavior, and retained shared delivery instructions in
  the rebuild guide.

- Reduced historical source prose to current lifecycle, security, and failure
  invariants without changing executable behavior.

- Removed unreachable LangGraph schemas/state, obsolete SSH container helpers,
  compatibility aliases, and unused scaffold/git/inventory code while retaining
  the live allocator, HTTP health probe, and BitLaunch policy boundary.

### Added

- The stand-only Definition-of-Done restart target now identifies terminal
  adoption by its fenced turn request rather than counting task attempts, so a
  later ordinary retry remains diagnostic instead of turning the restart proof
  red. Its PO stream lookup now uses the shared producer queue constant.

- Hardened the stand-only Definition-of-Done target against unrelated PO input,
  cold worker creation, and terminal worker history. Its operator acceptance
  fixture now reaches human review through the public story actions before it
  exercises the existing completed-story reachability guard.

- Added the stand-only Definition-of-Done live target for normal owner completion, audited
  `accept-result`, and an LLM `stand_token` engineering-consumer restart. It records the PO
  instruction boundary, acceptance audit and running-target refusal, then proves the retained
  broker turn, worker inventory facts, and absence of detached worker or workspace-lock state.

- Operators can drain the engineering consumer before a deploy through the admin Workers page. The
  credential-derived action persists and audits its drain state, leaves the existing ten-second
  shutdown handoff unchanged, and is honored by recreated consumers until explicitly resumed. A drain
  is checked after an entry is read as well as before slot reservation, leaving an entry read during
  the drain in the PEL for normal handoff. An unreadable drain poll retains the process-local last
  successful decision, so an API restart cannot reopen a drained consumer; before its first successful
  read, a recreated consumer fails closed and does not claim work. Repeated drain actions are audited.
  Worker inventory now
  shows Docker container, agent-process status, active turn lease, story binding, and waiting attempt
  as separate three-valued facts, making a live but unowned container visible instead of healthy and
  never presenting an unreadable source as absent.

- Engineering consumer rollouts now hand a reclaimed live turn to the replacement consumer instead
  of publishing a second prompt. The story-worker binding is written when the dynamic worker is
  acknowledged; the attempt's durable turn metadata and the broker lease identify the exact retained
  output, which carries the broker-owned request id. Shutdown still drains for ten seconds, then PEL
  reclaim adopts the turn or requests its deletion at its recorded deadline. Terminal settlement now
  centrally tears down every unconsumed recorded turn before its Run can become terminal, except for a
  worker owner-fenced to another engineering attempt; consumed typed output retains story-worker reuse.
  Reused-worker waits fence output by the current turn request id, cancelled waits remain reclaimable,
  adoption ignores malformed historical output for another request, and worker registration uses the
  consumer's story id rather than a branch-name convention. Recheck-deploy recovery now shares QA
  handoff's five-minute age fence, closing the commit-to-dispatch-stamp duplicate window.

- Custom Telegram bot audiences now retain the verified sender alongside any
  dictated IDs, and private audiences retain the project owner unless the
  persisted `allow_ownerless_audience` opt-out is set by an internal service
  acting for itself. The PO confirms one structured audience/language/
  must-requirements summary before it creates a story.

- Operators can recheck a typed, repaired QA infrastructure blocker from a `waiting_human_review`
  story. The audited action derives its actor from the credential, retains its basis and prior
  quarantine evidence, and is idempotent within the current quarantine episode: a queued or running
  recheck refuses repeats, while a terminal Run or a fresh typed quarantine enables another recovery.
  Both stopped and running targets return through deploy and ordinary QA, so the post-repair route has
  one verified deploy provenance. A persisted recheck handoff is recovered by the deploy supervisor
  if publication fails after commit. A passed recheck clears the quarantine, completes the story through
  the existing notification seam, and preserves QA's verified address even if a later health probe
  briefly marks the application down. Recheck rejects ambiguous multi-application projects and targets
  still stopping; standalone E2E rejects a matching quarantined story, while standalone redeploy remains
  the restore route for a quarantine Recheck QA cannot repair because it cannot produce a green QA verdict.

- Operators can accept a `waiting_human_review` story directly from the admin UI with a required
  basis. The authenticated shared admin-console account or LK bearer administrator, basis,
  acceptance time, and overridden quarantine evidence are stored with the story; `/complete` cannot
  bypass that audit. The obsolete quarantine evidence is cleared, ordinary completion notification
  recovery still delivers through `po:input`, and an address is included only for a currently running
  application from the current post-reopen work cycle. When that handoff exists, acceptance refuses if
  its application is not running and directs the operator to Recheck QA, so an owner is never told the
  product is ready without a reachable address; escalations before QA remain explicitly address-less.

- Completing a story now writes its durable `story_completed` PO notification on
  the story in the same transaction. Recovery delivers it through `po:input`
  for both QA and direct operator completion; a passed QA handoff preserves the
  verified deployment URL and bot username.

- `python -m shared` is the canonical broad unit suite entry point: it runs the
  tree's own `scripts/test-unit-local.sh` (same `ALL_SUITES`, same fixture env)
  with the running interpreter's venv first on PATH. Secretary workers use
  `check broad --reuse --module shared`, whose receipt records in-tree import
  provenance and is reused on unchanged content; the shell-wrapped
  `make test-unit` receipt never was.

- Stand e2e now invokes the selected suite through `scripts/stand_run.py` on
  the dynamic orchestrator, retaining deterministic JUnit alongside its TSV and
  logs. A fresh exact-tag cleanup runner records deletion selection, final
  provider inventory and `servers_used`, then builds one redacted acceptance
  artifact with observed per-machine lifetime and BitLaunch hourly-cost-derived
  run cost. Evidence fails closed on incomplete cleanup or a credential-shaped
  artifact, including a supplied redaction canary.

- The ephemeral stand now uses a non-secret `stand_token` worker auth selector:
  worker-manager resolves Claude and Codex credentials locally, Claude refuses
  an `ANTHROPIC_API_KEY` conflict, and Codex logs in from stdin without a host
  profile. Claude's opaque annual token is checked through strictly parsed,
  operator-supplied expiry metadata; Codex uses JWT expiry. The shared validator
  also drives the stand executor diagnostic, so valid token mode reaches paid
  worker admission while unavailable credentials fail closed. The stand workflow
  validates these prerequisites, Telethon and SSH material before the BitLaunch
  lifecycle preflight can create either machine.

- Stand e2e now bootstraps its dynamically created orchestrator from the
  checked-out revision, derives the public orchestrator identity only from the
  lifecycle outputs, and waits for the local API before it registers a separate
  pending BitLaunch target. The target carries its exact provider ID, IP, run
  tag and role; BitLaunch provisioning proves that current binding before it
  changes state, uses the stored creation key through the existing access and
  software playbooks, and cannot force-rebuild. The API provision request now
  dispatches the existing provisioner queue path, and the obsolete `stand-self`
  registration script and Make target are removed.

- Destructive server operations now use the provider-owned provisioning policy.
  Time4VPS rows carry `labels.provider="time4vps"` and their stable provider ID;
  scheduler sync upgrades only legacy rows that it matches by that stable ID.
  Rows without an explicit provider identity fail closed and are never adopted by IP.

- Stand e2e now creates a bounded, run-owned BitLaunch orchestrator/target pair
  through a provider-neutral lifecycle. Preflight refuses insufficient balance,
  quota, and SSH-material failures before creation; redacted manifests record
  machine observations; exact run-tag cleanup and a tagged TTL sweep cover
  successful, failed, and cancelled workflow paths. The obsolete static-host
  self-target route is removed.

- The production admin Dashboard now reads one strict internal/admin overview
  contract. It reports every declared queue binding with explicit degradation,
  all task-status counts, paid queued/running work, persisted executor-decision
  counts, and a newest-first bounded list of safe failed-Run errors. Legacy or
  malformed executor snapshots are marked unavailable rather than inferred.

- Worker-manager now publishes bounded, credential-safe Claude and Codex
  availability snapshots to Redis at startup and periodically. Admin Settings
  shows status, local auth mode, freshness, active leases and safe reasons.
  Paid admission reads the selected executor's current snapshot before any
  reservation or Run creation, refuses proven-unavailable executors, and
  requires a version-bound administrator confirmation for `unknown`.

- Internal/admin Settings now exposes one typed, audited paid-work control
  state: emergency stop, concurrent paid-run ceiling, and independent
  engineering/QA executor overrides. Overrides are `none`, `claude`, or
  `codex`, take precedence only for new Runs, and reset to the legacy policy
  without restart or deploy.
- Paid engineering and QA runs now receive an immutable typed executor decision
  at admission. Engineering preserves valid project pins or the API default;
  QA uses the API-side Codex-default setting. Worker launchers read the
  persisted decision by Run id, so later configuration changes cannot switch a
  queued attempt.
- Users can now inspect their ledger-derived engineering balance with the
  Telegram `/balance` command. It shows exact known spend and the API-calculated
  available amount without exposing the internal reservation split, and warns
  when cost coverage is incomplete.
- PO now has a self-only balance tool and must check it before creating or
  reopening paid work. It warns before a single-attempt reservation would
  exhaust the available amount and does not start work that the budget gate
  would reject.
- One-time promo-code registration that atomically arms an enabled engineering budget policy.
- Count-based work admission for projects and concurrent engineering/QA runs,
  with an internal/admin emergency-stop API and durable typed decision audit
  records. Paid runs now start through one transactional API command, which
  holds the counted slot through queued-Run creation and checks engineering
  money only after the count gate.

### Fixed

- The main-only backend Docker-in-Docker integration compose now supplies
  worker-manager's required internal API key, allowing the service to start.

- API actor guards now require the request credential when resolving a caller.
  LK bearer requests are always judged as the token subject, while
  `X-Telegram-ID` may name an actor only for an internal-key service caller.
  Project ownership/admission and allocation administration now use that same
  principal decision rather than a header-only user lookup.

- Stand image resolution now checks the deployed revision's release marker through
  a typed, read-only GHCR manifest response: only HTTP 404 falls back to a local
  build. Creation teardown carries a distinct reason that preserves execution
  retries, Factory requires and forwards its API key in every supported auth mode,
  and scheduler's shipped and isolated-test cadence both use the allocation
  freshness policy.

- Registry token and release-marker lookups now share one response matrix. It
  reports curl tooling separately from transport/DNS, rejects auth, rate-limit,
  and unexpected HTTP responses without authorizing a build, and accepts Docker
  and OCI manifests or indexes when confirming an existing release marker.
  `creation_failed` is documented as a `DeleteWorkerCommand` teardown reason.

- Worker creation failures now retain their terminal status, ownership metadata,
  and developer workspace fence until `delete_worker` confirms container removal.
  BitLaunch access provisioning uses the shared stable-ID parser while generic
  destructive operations remain denied; stand worker-image fallback now runs
  only for a confirmed missing release, and health-check cadence is validated
  against the allocator freshness policy at runtime.

- Stand acceptance admission now rejects structural private-key PEM markers in
  its existing allow-listed evidence, including literal, escaped, and serialized
  headers in raw suite combination logs. The shared scanner still permits
  value-free token-preflight diagnostics and reports only safe paths and reasons.

- Stand acceptance admission now derives redaction needles only from a fixed
  protected-value allow-list, so public dynamic configuration and lifecycle
  manifest values remain uploadable. Both handoff and final artifacts use an
  explicit `always()` admission gate, retaining scanned diagnostics for failed,
  cancelled, and incomplete runs while a failed admission still blocks upload.
  Value-free preflight diagnostics now remain admissible; both admission jobs
  require every protected value, and rejected decisions report only their safe
  relative path and reason in the job summary.

- Stand acceptance evidence now installs the pinned `uv` runner through the
  dynamic host provisioning path, captures remote runner diagnostics into the
  scanned handoff, and invokes the suite only after successful target
  provisioning. Both handoff and final artifacts have attempt-scoped names and
  are blocked until their exact allow-listed candidates pass a value-free scan.

- Stand cost evidence now retains BitLaunch's per-server `rate` in USD*1000 per
  hour and labels rate/time figures as rounded-hour estimates. A charged cost is
  reported as actual only for an exactly correlated documented usage row; bad
  rate, timestamp, cleanup, or run-shaped usage evidence remains incomplete.

- Stand token validation now centrally binds either GitHub runner or rendered
  stand-host credential names before checking expiry. `make stand-preflight`
  and `make stand-run` load the rendered stand configuration, so a valid
  `STAND_*` token configuration no longer fails as missing host-session state.

- Provisioner key-authentication now materializes every supplied SSH private
  key with exactly one terminal LF before invoking Ansible, so BitLaunch
  creation keys and later stored keys remain usable when secret storage omits
  the final newline.

- Deployment now maps the existing Time4VPS GitHub secret to the provider-scoped
  runtime policy key and validates that same key in contour guards and operator
  tooling. Scheduler discovery now refuses an IP collision without an exact
  provider/stable-ID match, reserves and unmanages the legacy row, and raises a
  critical administrator alert instead of creating a shadow server. A settled
  refusal is quiet on later sync cycles, while a later state change is refused
  and signalled again.

- The main-only backend DinD suite now gives worker-manager the same test-owned
  Claude host-session volumes used by launched workers and seeds fake
  refresh-capable credentials. The production validator remains fail-fast,
  while integration workers no longer fail before their mount/lifecycle tests.

- Queue inspection now marks incomplete Redis consumer-group observations as
  degraded instead of inventing zero consumers, pending work, or a delivery
  id. The Dashboard has a reachable zero-work empty state and its admin
  contract gate now recursively checks server schema fields, requiredness,
  nullability, enums, maps, arrays, and nested paid-work diagnostics.

- Unknown executor-diagnostic confirmation now requires an LK bearer for the
  actual administrator, so an internal service credential cannot impersonate an
  admin through `X-Telegram-ID`. Terminal pre-container worker refusals now
  settle as zero leases while nonterminal and Docker/Redis lifecycle
  disagreements continue to fail closed as unknown.
- Executor diagnostic records now reject contradictory enabled, auth-mode,
  availability, lease and reason states at the shared boundary. Worker-manager
  now reconciles Redis and Docker inventories in both directions before it
  reports lease counts, preserving reconciled live leases for disabled
  executors.
- Executor diagnostics now require the complete protocol version at the Redis
  boundary, map expired snapshots to typed unknown, derive response text from a
  fixed safe reason mapping, and keep unknown inventory lease counts null. The
  Claude validator uses worker-manager's `/host-claude` mount without changing
  the Docker-host source used for launched workers. A confirmed unknown is
  re-read immediately before paid admission so a newer unavailable snapshot
  cannot reuse the earlier confirmation.
- Executor-availability integration fixtures now publish complete fresh
  diagnostics, the worker-manager rollout suite keeps its runner alive across
  intentional control-plane replacement, and Settings updates stale state
  outside render.
- Deploy seeding now initializes absent paid-work controls through a distinct
  typed operation and never replaces live emergency-stop, paid-run ceiling, or
  executor-override values. Settings confirms every executor override
  transition, including reset to the legacy policy.
- Production admin access now binds only to `127.0.0.1:3001` for SSH forwarding;
  Caddy does not expose it. Analytics, agent-configuration, service-deployment,
  and queue-debug routes now require an administrator or internal service, while
  nginx Basic Auth and its required admin credentials continue to protect every
  admin surface.
- Updated paid-run test fixtures to use the transactional paid-run command;
  fixtures that only require a Run record now use a non-paid type.
- Paid-run starts now replay an identical stable command idempotently and reject
  conflicting payloads; the obsolete standalone paid-work admission oracle was
  removed. Emergency-stop writes are strict booleans and admission controls are
  protected from the generic configuration mutation API.
- Paid-work refusals now persist their command identity, project owner, typed
  reason and Russian owner-facing text without caching a transient outcome.
- A paid-run retry now rechecks controls rather than caching a prior refusal;
  it only reuses a live Run whose engineering reservation remains active.
- Scheduler admission refusals now park their Task as well as the Story, and a
  handled pre-handoff publish failure closes the Run with its released hold.

## 2026-08-24

- Released engineering-budget reservations no longer replay their historical
  `admitted` outcome as authorization. A repeated deterministic dispatch now
  reacquires the policy lock and re-evaluates ledger spend plus chargeable
  holds before atomically re-arming the existing reservation or denying it.
  This preserves the hold through a deploy-fix retry after a proven
  pre-handoff failure, including unknown terminal cost, without minting a new
  attempt identity.

- Supervisor deploy-fix redispatch now uses the same durable engineering-budget
  admission before it creates a Run or publishes work. A denial records the
  balance context, writes a typed `story_quarantined` owner notice, then parks
  the story in human review before attempting delivery; pre-handoff failures
  release the exact deploy-fix reservation, while published attempts continue
  to terminal known or unknown-cost settlement.

- PO owner-notification events now use one shared typed vocabulary. The
  scheduler's durable and direct owner-message producers and the PO consumer
  import it, `POSystemEvent` rejects arbitrary event names, and a durable
  notification cannot be marked delivered for an event PO would drop. The
  deploy-fix budget-denial path reuses `story_quarantined` with budget-specific
  text, so its persisted reason, owed record, human-review transition and
  recoverable delivery remain in that order.

- Engineering-budget dispatch recovery now treats queue publication as the only
  handoff boundary: manual and scheduler failures before it release active holds,
  including local refusals, Run creation and recipient resolution. Scheduler
  budget denials move tasks through the legal `in_dev` transition to
  `waiting_human_review` with auditable balance context, preventing polling
  redispatch until an explicit human resume.

- Engineering dispatch now makes a durable, server-authoritative budget admission before
  creating a Run or touching the engineering queue. Per-user policy locks aggregate
  immutable ledger cost with active and unknown-final reservation holds; zero available
  budget denies, and repeated attempt identities retain their first decision. Publish
  failures release their pre-handoff hold, while terminal known costs settle it and
  unknown terminal costs retain conservative coverage without being called actual spend.

- Added durable per-user engineering-budget policies and a ledger-derived balance
  API. Policies use integer micro-USD limits, typed enabled/disabled state and
  optimistic versions; internal/admin writes are idempotent at the requested
  state and reject stale mutations. Self-only reads and named admin/internal
  reads expose exact known spend, unknown-cost coverage and the explicit
  unlimited/not-enforced distinction without adding a mutable spend counter.

- Factory result normalization now clears partial provider totals and derives a
  coherent total only from valid input and output components, so malformed
  usage cannot reject a terminal ledger write. Factory evidence with no valid
  reported model now retains the selected configured model at the DeveloperNode
  profile seam; a valid result model still takes precedence.

- Factory `droid exec -o json` terminal results now retain a single typed
  `type=result` object's available non-negative model, token, and cache facts
  through the existing worker-result path to the engineering-attempt ledger.
  Factory money-looking fields are discarded, so Factory and Codex attempts
  keep explicit unknown cost with NULL micro-USD while retaining selected
  provider/model profiles. Codex output remains unparsed.

- Claude terminal results now carry one typed provider-evidence object from
  worker-wrapper through the existing worker-result and terminal Run paths to
  the append-only engineering-attempt ledger. Claude JSON money is parsed as
  `Decimal` and converted to integer micro-USD before the queue boundary;
  cache-read and cache-write tokens are retained. Invalid monetary evidence is
  explicit unknown cost, never zero or an inferred tariff, while valid facts
  from the same result remain available. The ledger rejects mixed or
  contradictory Claude facts, and terminal retries still use its first-write-
  wins writer.
  - Serialized worker results may retain null legacy metric placeholders beside
    Claude evidence. Their transport validation ignores only those nulls,
    retains rejection of non-null mixed facts, and revalidates HTTP-attached
    evidence as the typed object before broker submission.

- Added the append-only engineering-attempt ledger. Terminal engineering Run
  updates write one idempotent record under the existing Run row lock,
  attribute project-bound attempts from `Project.owner_id`, preserve unknown
  cost explicitly in integer micro-USD accounting, and expose filtered,
  ownership-checked read-only ledger access at `GET /runs/engineering-attempts`.
  Backfill skips in-flight Runs, and retained Run projections are bounded.
  Hard deletion of a project detaches its ledger's deleted lifecycle links
  while preserving its immutable accounting history and resolved user owner.

## 2026-08-21

- Telegram bot audiences can now be changed conversationally, one user at a
  time. `set_bot_access` stays for the initial public/custom/only-me choice,
  but it replaces the whole audience, which made "add user 84" through a
  conversation unsafe: an LLM reconstructing the comma-separated list could
  silently drop IDs. New typed endpoints
  `POST /projects/{id}/config/bot-access/users` (body `{"telegram_id": N}`) and
  `DELETE /projects/{id}/config/bot-access/users/{telegram_id}` mutate the
  stored audience under the project row lock, validate numeric IDs,
  deduplicate, preserve unrelated config/secrets and enforce ownership like
  every other project mutation. Removing the final allowed ID is refused — an
  empty private audience is the public bot, so going public remains an explicit
  `set_bot_access(mode="public")` decision. Repeats are idempotent: adding a
  present ID or removing an absent one persists nothing and launches no rollout.
  - A mutation of an already-deployed bot stages a config-only rollout: the
    latest successful service-deployment record supplies the running
    application and its deployed SHA, and a plain PO-triggered FEATURE deploy of
    that same SHA re-reads the project's `env_overrides` in the DevOps subgraph
    and ships the new audience — no story, no engineering, no rebuild, no new
    deployment system. If something is running but no deployment recorded its
    SHA, the endpoint refuses with 409 rather than pretending; if nothing is
    deployed it reports `not_deployed` and only persists the configuration.
  - The response separates the write from the effect (`rollout`:
    applied/pending/failed/not_deployed with a durable run id), and a status
    endpoint reads that run back. The PO gained matching `add_bot_user` /
    `remove_bot_user` tools that poll the rollout to a bounded verdict and are
    required to report applied, pending and failed differently — access is
    never called live merely because the transaction committed.

- Bot-audience rollout hardening, after review:
  - `set_bot_access` now goes through the same mutation/rollout orchestration
    as the one-ID endpoints, so a public/private/custom switch on a running bot
    also reaches the live containers instead of stopping at the database.
  - The rollout target query is bound to the requested project through its
    repositories — it can no longer pair this project's name with another
    project's application or SHA. Multiple running applications resolve to the
    most recently successful deployment; running-but-unattributable refuses
    with 409.
  - The rollout status endpoint proves `run.project_id == project_id` in SQL
    and then applies the canonical project access check: another owner's (or
    another project's) run id reads exactly like a missing one. Rollout runs
    are stamped with the owner for audit.
  - The commit/publish gap is closed by a durable publish-intent record on the
    run (`shared/contracts/bot_rollout.py`), committed before any queue write.
    The API settles the record to published right after its own write lands; a
    scheduler sweep (`services/scheduler/src/tasks/bot_rollouts.py`) retries
    only what never made it — until the stream accepts or three attempts are
    spent (then an admin alert, and the run leaves the sweep's selection). A
    retry that finds an unchanged-but-unapplied audience resumes this work
    instead of saying "nothing changed".
  - The PO tool's synchronous wait now fits the Telegram transport window
    (~40 s inside the 60 s response-stream TTL). If the rollout is still
    pending when the wait ends, an idempotent notify-owed marker is written on
    the run and the sweep delivers the terminal outcome proactively — applied
    or failed — so the user hears the ending even though the reply went out
    earlier.
  - Transient status-poll errors retry until the bounded deadline instead of
    reading as a verdict; undeploy supervision skips rollout deploy runs so a
    config redeploy cannot be misread as a teardown success/failure; the
    bot-access domain moved out of the projects router into
    `src/utils/bot_audience.py` + `src/routers/_bot_access.py` with shared
    guards in `src/routers/projects_guards.py`.
  - Real-SQL regression tests cover cross-project target isolation,
    cross-project/cross-owner status denial, the publish-owed record and sweep
    selection; scheduler unit tests cover publish retry, exhaustion+alert and
    one-time terminal delivery.

- [hotfix] A manual CI dispatch for `main` now runs the required backend
  Docker-in-Docker suite as well as a main push. The release gate deliberately
  refuses a skipped DinD result on `main`; the job had been limited to `push`,
  which made a manual candidate or retry run fail closed for the wrong reason.

- [hotfix] Worker images now ship the `shared.constants` module that
  `worker-wrapper` imports for its turn timeout. The common base image had copied
  only contracts, logging and diagnostics, so every worker crashed with
  `ModuleNotFoundError` before its agent CLI could start. A Docker-in-Docker
  runtime regression now imports the wrapper from the built image itself while
  retaining worker isolation: no Redis or other control-plane module is added.

- [hotfix] A backend Docker-in-Docker failure can no longer release worker
  images. The main-only worker runtime suite moved from a parallel workflow into
  the `ci.yml` DAG; `Required CI Gate` now consumes it, and the worker marker
  publisher remains downstream of that gate. A failed, cancelled or skipped DinD
  job on main therefore leaves the SHA without a release marker and production
  deploy refuses it before changing running services.

- Engineering worker supervision now follows the input lease rather than a
  wrapper heartbeat or `task.updated_at`. The broker records the active turn
  with its worker, attempt, request, lease and absolute deadline; a reused
  worker cannot lend an old turn's activity to a new attempt. Missing Redis
  status is unknown, not Docker death. A deadline writes a durable stop intent
  and requests deletion, but a task becomes retryable only after worker-manager
  has recorded successful container removal. Workspace locks are owner-fenced,
  and the wrapper kills the agent's whole process group while retaining its
  partial transcript. `AGENT_TURN_TIMEOUT` defaults to 60 minutes; outer waits
  are derived from that limit plus bounded teardown overhead.

- The PO consumer no longer keeps its own copy of the read loop. It was the last product consumer
  reading `po:input` through a private `XAUTOCLAIM` copy, and it still carried exactly the defects
  already fixed in `shared/redis/client.py`: reclaim only at start-up, a sweep that stopped on the
  first page it could not claim from, a silent skip for a body-less claim, and an `XACK` that
  destroyed a body it could not validate. The copy is gone — PO now reads through
  `RedisStreamClient.consume_typed`, so the continuous sweep, the full PEL walk, both `XAUTOCLAIM`
  response shapes, `stream:diagnostics:lost_entries`, the `po:input:dlq` quarantine before ACK and
  the tolerance for a field a newer publisher added all come from the one implementation.
  - **Concurrent dispatch is covered by the ids the loop has in flight.** PO processes in
    `asyncio.Task`s under a semaphore and per-user locks and ACKs in the task's `finally`, so an
    entry is legitimately pending for as long as the graph runs, and this process's own sweep finds
    it idle past `PEL_TIMEOUT_MS` and hands it back. The read loop recognises such an id and does
    not start a second `_process_message` for work already running. The id is dropped when the task
    ends, success or failure, so an entry whose ACK raised goes back to ageing towards a reclaim,
    and an entry left in flight by a process that died is claimable by the next PO after one
    `PEL_TIMEOUT_MS`. `RECLAIM_INTERVAL_MS = PEL_TIMEOUT_MS // 2` is a sweep period and nothing
    else — it bounds the pickup delay for a genuinely stuck entry at 1.5 timeouts instead of 2,
    because `po:input` is what a waiting user is on the other end of.
  - **The delivery contract for `po:input` is the at-least-once the other six consumers have.**
    The in-flight set lives in one process and excludes a second dispatch *there*; it cannot
    exclude another PO process, whose sweep may claim an entry that has been pending for
    `PEL_TIMEOUT_MS` whatever this one is doing with it. Mutual exclusion between PO processes is
    deliberately not built: it needs ownership with fencing and a way to cancel the running graph,
    which is a decision about the graph's external effects rather than a Redis setting, and
    `langgraph` has no `deploy.replicas`. An overlap would not be silent — the entry stays in the
    PEL and its delivery count grows.
  - `CONSUMER_NAME` now carries the hostname as well as the PID. Two standard containers are both
    PID 1, and two processes answering to one consumer name share a PEL, so each would have read
    the other's in-flight entries as its own and neither the PEL nor a log would tell them apart.
  - The existing secret elision is untouched: a validation failure still logs `loc`/`type` only,
    the raw body never reaches a log, and the alert for a message still addressed by the removed
    `user_id` field is raised by the same shared path.
  - The `XAUTOCLAIM` scan model the sweep tests need (`RedisPelScan`) moved to
    `shared/tests/redis_pel_scan.py` so the shared-client suite and the PO suite share one copy.
  - `docs/CONTRACTS.md`, `docs/ERROR_HANDLING.md` and `ARCHITECTURE.md` no longer describe PO as
    the documented exception to the unified consumer path.

- A stream entry no longer disappears without a trace. Three defects in `shared/redis/client.py`
  shared one failure shape — the record is gone and there is nothing to look at — and are closed
  together because they patch each other's hole.
  - **PEL reclaim is now continuous.** The `XAUTOCLAIM` sweep used to sit *before* the
    `XREADGROUP` loop in `_iter_entries`; the generator is created once per process, so it ran at
    start-up and never again, and an entry stuck afterwards waited for a service restart while six
    consumers were written expecting the opposite. The sweep now runs inside the loop, every
    `reclaim_interval_ms` — by default `pending_timeout_ms` floored at 1s. `min_idle_time` is
    unchanged, so a healthy consumer's in-flight entry is still not taken from it.
    A sweep also follows the `XAUTOCLAIM` cursor across pages that claim nothing: Redis stops
    scanning after about `COUNT * 10` PEL entries, so a prefix of entries a healthy consumer is
    still holding used to end the sweep and hide every stale entry behind it.
  - **A poison entry is quarantined, not destroyed.** `consume_typed` copies an entry it cannot
    decode or validate to `{stream}:dlq` — source stream, group, entry id, failure kind, the
    structured reason with values elided, and the body verbatim — and ACKs it only once that copy
    lands. A failed DLQ write leaves the entry in the PEL for the next sweep. The DLQ carries the
    body because it is a stream in the same Redis the payload already sat in; the logs keep their
    elision, and nothing there was weakened.
  - **A trimmed entry is diagnosed.** `MAXLEN ~` on publish and the scheduler's `XTRIM MINID`
    both ignore the PEL, so `XAUTOCLAIM` reports pending entries with no body. Both response
    shapes (Redis 6.2's `(id, None)` and Redis 7's deleted-id list) are now logged as
    `stream_entry_lost_to_trim` and counted in `stream:diagnostics:lost_entries`, instead of a
    silent `continue`.
  - **A newer publisher no longer destroys the message.** A payload that fails validation only
    because it carries fields the consumer's schema does not know yet is accepted with those
    fields dropped and the names logged. Everything else — a missing field, a wrong type, an
    unknown `QueueMeta.version` — still fails and takes the DLQ route. The tolerance is read-side
    only; contracts keep `extra="forbid"`, so a publisher still cannot emit an unknown field.
  - `docs/ERROR_HANDLING.md` (PEL Recovery, Error handling flow, DLQ) now describes what the code
    does; the DLQ's "(if implemented)" is resolved, with the paths that still have no DLQ named.

- Re-delivering a scaffold message onto an already-scaffolded workspace now succeeds instead of
  destroying the project. `copier --overwrite` rewrites byte-identical files, so `git commit` exits
  non-zero with nothing staged; the git step used to break out of its loop there, never reach
  `git push`, and report `Git push failed`. "Nothing to commit" is now told apart from a real
  commit failure by an empty `git status --porcelain` — machine-readable and locale-independent —
  and the push still runs, so the second pass ends with `success == True`. `commands_log` and the
  structured log now distinguish `git commit: rc=0 (committed)` from
  `git commit: rc=N (nothing to commit)`; a genuine commit or push failure still fails the scaffold
  with diagnostics.

- A failed scaffold no longer fails every story of the project. `_process_full_mode` fails only
  stories still in `created`; work already in flight (`in_progress`, `pr_review`, `deploying`,
  `testing`, `waiting_*`) or already finished (`completed`, `archived`) is left alone, and the
  failed/skipped counts are logged. Covered by a two-pass idempotency test that runs the real git
  commands against a local bare remote.

## 2026-08-20

- [hotfix] Ensure-workspace jobs for active projects now clone the linked Repository name instead
  of reconstructing a GitHub repository from `Project.slug`. Imported projects may retain an
  existing repository whose name predates the orchestrator project record; using the generated
  slug made scaffolding report that repository missing, persisted `scaffold_error`, and left every
  story task in `todo` before a developer worker could start. Full scaffolding keeps using the
  generated project slug for genuinely new repositories.

## 2026-08-15

- [hotfix] The PO-default matrix preflight now binds its PO tool to the checkout it is proving.
  `uv run` puts every workspace member's sources on `sys.path` and `services/api` precedes
  `services/langgraph` there, so a conditional insert was a no-op and `import src.agents...`
  resolved to the API service — the preflight died on production with `No module named
  'src.agents'`. The checkout is forced ahead of the workspace entries and the imported module's
  provenance is verified against it, so a wrong binding is a named preflight failure. Covered by a
  child-interpreter regression that performs the import the workflow actually performs; the
  existing runtime tests all build `MatrixRuntime` with `object.__new__` and never executed it.

- The operator-dispatched Production Agent Matrix now runs one PO-default preflight before its
  worker/QA combinations. The preflight invokes the released `create_project` tool with a real
  test-user RunnableConfig and no `agent_type` argument, reads the sidecar API's actual runtime
  default and persisted project, then creates a separately owned project with a different explicit
  supported agent. It retains one exclusive, SHA-bound redacted receipt with the project,
  repository, initiating-run, PO-response and notification-probe identifiers. Both projects are
  registered in separate live manifests before the tool calls and pass through the normal
  run-scoped cleanup path on success and every error path; response/proactive/outbox ambiguity is
  a failed preflight, never a silent absence.

- PO-default preflight notification evidence now takes a `po:proactive` stream boundary before
  either manifest-owned project is created and examines only the post-boundary delta. Historical
  harness-user deliveries are recorded only by the boundary and cannot poison a later operator
  dispatch; a delta entry for either owned project or one without a valid project identity fails
  closed. Owned delta entries are registered in their manifest for normal cleanup. The preflight
  also upserts the established harness identity itself, reserves initiating-run space for the
  `-explicit` variant, and records the underlying failure class and redacted message in its receipt.

- The Backend Docker-in-Docker Claude-agent checks now own each worker through
  create completion, sustained Docker liveness and worker-manager deletion.
  `test_claude_instructions_injected` uses API-key mode because it verifies
  injection rather than session persistence; a stopped worker now reports its
  status, exit code and bounded log tail instead of retrying a Docker 409. The
  deliberately non-writable regression waits for its owned worker to exit
  before it checks that evidence, so it does not race the positive sustained-
  readiness assertion. The actual host-session check uses a test-owned,
  worker-writable DinD volume.

- Application undeploy and live-target recovery now stream one project-scoped cleanup script. It
  captures Compose-labelled images, volumes, networks and anonymous volumes before `docker compose
  down`, accepts directory-less image residue only for exact `<project>-{backend,tg-bot,frontend,
  notifications-worker}` tags, refuses a candidate image with another tag or any remaining container
  reference, verifies each selected artifact is gone, and only then removes `/opt/services/<project>`.
  The candidate snapshot is retained under `/opt/services/.codegen-cleanup-candidates/` until success,
  so a live reference that leaves an anonymous volume behind is retryable after its source container
  and even its service directory have gone. The Backend
  Docker-in-Docker regression proves removal, a live neighbour and reusable postgres/redis tags
  surviving, idempotent retry, and a retained retry directory for a referenced anonymous volume.
- QA's Telegram capability now returns and persists typed reply evidence: separate text and caption, media type, reply-keyboard/inline-button data, callback answers with only post-press bot replies, and post-press evidence of the clicked message so edit-in-place is observable. Link previews retain their text. Inline callbacks are accepted only for a button the same QA run observed from its bound bot; a refused callback is stored as an undelivered non-product blocker, and the executor still receives no Telegram credential.
- A Telegram capability error now overrides an agent's product verdict with a typed QA blocker. An operation proven undelivered, including Telethon's empty-message `ValueError`, is stored as `telegram_probe_undelivered` with attempted, sent and received evidence; an ambiguous capability error stays an `unknown` blocker. The supervisor creates engineering fixes only from typed failed checks without a blocker and otherwise stops for human review.

## 2026-08-13

- The Backend Docker-in-Docker suite reaches its own assertions again. The three run-scoped suites
  load `tests/live/run_evidence.py` and `tests/live/run_cleanup.py` by path — `tests/live` is not a
  package — but did not register the loaded module in `sys.modules`. Both modules declare
  dataclasses under `from __future__ import annotations`, and `@dataclass` resolves string
  annotations through `sys.modules[cls.__module__]`, so executing them unregistered raised
  `AttributeError: 'NoneType' object has no attribute '__dict__'` from `dataclasses` itself before
  any test body ran (15 errors). Both loaders now register the module before executing it.

- The periodic orphan sweep in `services/worker-manager/src/garbage_collector.py` never removes a
  container that is still alive. Redis was its only evidence, so a lost `worker:status:*` — a flush,
  a restart without persistence, a wiped volume — made every live worker of every run look like an
  orphan and the 30-minute sweep deleted it mid-run. A container in `running`, `paused` or
  `restarting` state with no Redis entry is now kept and logged (`orphan_gc_keeping_live_container`)
  instead; exited and dead orphans are reclaimed exactly as before. The protection travels with the
  worker id: a live worker's QA-egress proxy and its `dev_proj_<worker id>` network are kept too, and
  a proxy that is itself still serving is kept whatever state its worker is in.

- A run's cleanup is driven by its ownership labels, not by a reconstructed context.
  `tests/live/run_cleanup.py` takes a run id and removes that run's worker containers, its
  QA-egress sidecars and its dev networks with `docker ps -a`/`docker network ls --filter
  label=com.codegen.run.id=<run>`, so resources are found whether or not Redis still knows them and
  whether or not the harness that created them is alive. It is idempotent — "already absent" is a
  success — and it verifies afterwards with the same two queries, raising `RunCleanupError` if
  anything for that run remains. `scripts/clean_live_tests.py` runs it for every manifest before the
  `ctx` round-trip it used to depend on (`issue:6b4cae67568ff1d8bf82`).
- The label is the fence as well as the finder: a listed resource whose `com.codegen.run.id` is not
  this run is refused rather than removed, and long-lived service containers carry no run label at
  all, so a cleanup scoped to one run cannot take a neighbouring run's resources with it. Proved
  with both runs present at once against a real daemon in
  `tests/integration/backend/test_run_scoped_cleanup.py`.
- `dev_proj_<worker_id>` networks now carry the same ownership labels as the worker they belong to,
  plus `com.codegen.type=worker-dev-network`. A network's name is derived from a worker id, and a
  worker id is exactly what is unrecoverable once the container and `worker:meta:<id>` are gone;
  labelling the network at creation is what makes it findable by run afterwards.
- `worker:meta:<id>` retained because a worker's removal record could not be stored is removed only
  once that run's evidence accounts for the worker — that is, the run's evidence collector holds a
  record for it (`RunEvidenceCollector.accounted_workers`), so the worker is in the artifact with its
  ending or with the stated reason its ending was unreadable. Otherwise the key is kept and named in
  the cleanup report as expected residue, never swept as an anomaly. A run with no evidence of its
  own takes a capture pass and retains it under `.live-manifests/evidence/<run id>.json` before
  anything is removed; `worker:evidence:removed:<run id>` is evidence and is never deleted by
  cleanup.
- A run has one evidence artifact and it only ever gains. `retain_evidence` merges into
  `.live-manifests/evidence/<run id>.json` instead of replacing it, keeping the record that knows
  more (an exit code first, then whatever else was read) whenever two passes describe the same
  worker, and keeping every pass's capture errors. Recovery makes two passes over a manifest — the
  run-scoped label sweep and then the `ctx` round-trip — and the second runs after the containers,
  removal records and metadata the first read are gone; overwriting would have erased the accounting
  that authorised their removal. An artifact that cannot be read, or that names another run, is
  refused rather than written over.
- Removal is fenced by accounting rather than merely preceded by a capture attempt. `clean_run`
  compares every listed container and network against `accounted_workers` and leaves in place — and
  fails with `RunCleanupError` — anything whose worker the run's evidence does not name, including
  that worker's Redis keys. A capture that failed with a transient Docker error is no longer a
  licence to remove what it could not read: `account_listed_workers` turns such a failure into a
  stated missed capture naming the worker and why its ending is unknown, which is an acceptable
  ending, and only that record authorises the removal. A silent disappearance is not an outcome the
  harness can produce.
- Cleaning a run has four phases in this order: fence, capture, remove, verify. Losing the harness
  stops nothing else — the API, the scheduler and worker-manager carry the project on — so a worker
  carrying the target run's label can still be working when an operator recovers a crashed harness.
  `scripts/clean_live_tests.py::recover_ownership_manifests` therefore establishes the pre-existing
  cancellation and quiescence fence *first*, through the harness's own
  `pipeline_helpers.fence_owned_work` (extracted from `cleanup_all`, which still uses it, so there is
  one fence and not two), and only then takes the run-scoped label sweep and the `ctx` round-trip. A
  sweep in front of the fence is not a faster cleanup, it is a cleanup racing the run it is cleaning:
  it would capture a running worker, account it and force-remove it while its run was still live.
  A manifest whose fence cannot be established is reported loudly and otherwise left alone — nothing
  of that run is captured or removed — which is the only case where refusing to clean is correct.
  `scripts/tests/test_clean_live_tests.py` drives the real recovery over a fake API and a fake daemon
  whose worker keeps running until its run is cancelled, and holds the phase order and the refusal.

- The cleanup adapter a crash recovery actually uses is now proved against a real daemon.
  `scripts/clean_live_tests.py` and `tests/live/pipeline_helpers.py` build
  `run_cleanup.docker_cli_ops`, which asks the daemon in Go templates and parses text back, while
  every real-daemon case so far ran `docker_sdk_ops`; the CLI adapter's templates were answered only
  by hand-written fixtures. `tests/integration/backend/test_run_scoped_cleanup_cli.py` runs the
  run-scoped scenario through the CLI adapter against the Docker-in-Docker daemon: a container
  carrying the run label listed with its name and labels, a container carrying the run label and
  none of the others — which proves `{{.Label "…"}}` renders an absent label as an empty field
  rather than a Go placeholder that would become a worker id — the `{{.Labels}}` `k=v,k=v` rendering
  `_labels_from_pairs` splits for networks, and a whole run removed while a neighbouring run on the
  same daemon survives. Without it a template that rendered differently on a real daemon would make
  the sweep find nothing and the verification pass then report the run left nothing behind: a false
  all-clear in the one check that exists to catch a failed cleanup. The integration runner image
  carries the docker CLI at the daemon's own version for this. The adapter's Redis half is not
  covered there: it reaches Redis as `docker compose exec -T redis redis-cli`, addressing the live
  compose project, which the runner's CLI — pointed at the nested daemon — is not in.

- The backend Docker-in-Docker suite runs on every push to `main`. The worker-ownership,
  run-ownership-propagation and run-evidence-by-label tests are the only real-daemon proof that a
  dead, unsampled worker is still attributable and that a run-scoped query excludes its neighbour,
  and they only ran when someone remembered to dispatch them. `backend-integration.yml` now also
  triggers on pushes to `main`; it stays dispatchable by hand and stays out of pull requests, where
  a privileged nested-daemon suite costs more than it protects. Its job and test step remain
  unconditional and are not advisory, so the run is red when the suite fails and cannot go green by
  skipping. It is a separate workflow from `Required CI Gate`, so it reports on `main` and does not
  block a merge. The CI contract asserts the trigger set, the branch and the non-advisory shape.

- Whoever removes a worker captures its ending first. Before `delete_worker` removes a worker's
  container it reads that container's exit code, a bounded and redacted log tail, its image, its
  agent type and the host directory its transcript was retained in, and writes them to
  `worker:evidence:removed:<run id>` — keyed by the ownership already on the worker and deliberately
  outside the `worker:meta:<id>` the same deletion goes on to delete. Labels survive a worker that
  died; they do not survive one that was removed, and no polling interval fixes that, so the
  vanishing point is where the capture belongs. A worker created and deleted before any observer
  looked at it now reaches its run's artifact with its exit code, not as a stated miss and not as an
  omission.
- Destructive steps are ordered by how much attributability they destroy. Removing the container
  always proceeds — cleanup is never wedged by observability — but `worker:meta:<id>` is the
  worker's last durable name, so it is deleted only once the removal record exists. When the record
  cannot be stored, `delete_worker` keeps the metadata and logs `worker_meta_retained_for_attribution`
  instead: the run's ownership manifest can still name the worker as an explicit missed capture, and
  a leaked key a label sweep collects later beats a worker no source can name.
- Capture never owns cleanup. It is bounded by `WORKER_REMOVAL_EVIDENCE_TIMEOUT_SECONDS`, it raises
  nothing at the deletion, and every fact it could not read becomes a stated reason in the record
  rather than an absence: a worker whose ending cannot be read is still removed. Records are kept
  for `WORKER_REMOVAL_EVIDENCE_TTL_SECONDS`.
- The run evidence collector (artifact schema v4) now reads three sources in order of strength: the
  containers the run label still lists, the removal records for those already gone, and — for a
  worker in neither, because the capture itself never reached Redis — the run's ownership manifest,
  which can still only add an explicit missed capture naming why.

- A dynamic worker's death is attributable, and the run finds its workers by label. Every worker/QA
  combination of the production matrix now emits one retained, machine-readable artifact
  (`docs/e2e_results/run-evidence-*.json`) naming the deployed SHA and the worker image digest
  record in use, the project, the role agents as executed, the attempts, the terminal state, a
  failure kind, and per worker container its exit code, a bounded log tail and the path of the
  transcript worker-wrapper already retained on the host. The matrix prints it per combination and
  records its path in the summary table.
- Discovery is `docker ps -a --filter label=com.codegen.type=worker --filter
  label=com.codegen.run.id=<run id>`, so a worker that exited — and whose `worker:meta:<id>` is
  already deleted — is still listed and still readable. Nothing depends on a poll landing while a
  container happens to be alive, which is what the previous attempt tried and could not make work.
  The QA executor creation-window heuristic and the developer container-name prefix are gone with it.
- What a label cannot survive is the removal of the container, so evidence is collected on every
  engineering poll and every poll of the post-deploy QA wait — always before cleanup — and the run's
  ownership manifest is reconciled in as a second source that can only add an explicit missed
  capture. A container listed running that then disappears, and a worker the run owned that the
  label query never listed, are both written as a stated reason naming what was lost. An omitted
  worker would read as "nothing ran", which is exactly the failure this evidence exists to end.
- A QA role is reported as exercised only once its worker handed a result to QA; a combination whose
  worker died first carries a QA cell that says so and why. The executor is reported from the QA
  container actually observed, never from the qa-worker's configured selector, which is recorded
  separately as the selection it was asked to make.
- The privacy boundary is unchanged: Codex CLI diagnostics still never enter the business result
  stream or service logs. The artifact's log tail is the worker container's own log, bounded and
  redacted against the container's secret environment values, and the transcript is referenced by
  path, never copied.

- Every dynamic worker is stamped with its owner when it is created. `WorkerConfig` now carries a
  required `WorkerOwnership` (project id and run id, both non-empty); worker-manager writes both to
  the container's Docker labels — `com.codegen.project.id` and `com.codegen.run.id`, next to the
  existing `com.codegen.worker.id` and `com.codegen.type` — and into `worker:meta:<worker_id>`,
  before the container is created. A worker that dies in its first second and has its Redis metadata
  deleted is still found and attributed with `docker ps -a --filter label=...`, which is what
  label-based crash cleanup and per-run evidence will be built on. Ownership can no longer be
  absent: a create command without it is refused by the contract, and `request_spawn` cannot be
  called without it.
- The QA executor is ownable on the same terms. `run_qa_executor` is handed the QA run's project and
  run id (`QAMessage.project_id` / `QAMessage.run_id`) and worker-manager records them; the run's
  egress proxy is labelled with the same run. The QA isolation boundary is unchanged: no git, no
  GitHub token, no repository, the internal QA network only, and the capability endpoint as its one
  route to the deployment. Because a QA executor now records a project, it is explicitly excluded
  from the developer workspace mutex — it neither takes the lock, nor is blocked by one, nor
  releases one when it is deleted.
- A worker's run is the run that *initiated* the work, and it enters the system in exactly one
  place: `Project.initiating_run_id`, supplied by whoever starts the run when the project is
  created (`ProjectCreate.initiating_run_id`, required and non-empty). Every producer of engineering
  and QA work reads it from the project and carries it on the message
  (`EngineeringMessage.initiating_run_id`, `QAMessage.initiating_run_id`, both required), and
  `WorkerOwnership.for_engineering` / `for_qa` — the only two places a worker's ownership is
  derived — turn that message into the ownership worker-manager stamps. A live run therefore finds
  its own dead workers with `docker ps -a --filter label=com.codegen.run.id=<manifest.run_id>`.
- The attempt is carried too, and separately. `com.codegen.attempt.id` (`WorkerOwnership.attempt_id`)
  is the engineering Run row a developer worker was spawned by, or the QA Run row its executor
  serves; one initiating run may spawn many attempts, so a run-scoped query must not be answerable
  only per attempt. `com.codegen.run.id` never carries an attempt id.
- The live harness names its run before it creates anything: `OwnershipManifest.run_id` is a fresh
  `live-…` identity (no longer the project id), it is what the project is created with, and the
  manifest file under `.live-manifests/` is named after it.
- Projects that predate run ownership name no run and are never given one. The migration adding
  `Project.initiating_run_id` does not backfill: the run that created such a project was never
  recorded, and any substitute — its project id, a minted id, a shared constant — would be stamped
  on its future workers as `com.codegen.run.id`, so a query scoped to one run would select workers
  belonging to another. The column is nullable, absence is refused at the one place it is read
  (`require_initiating_run`, raising `ProjectPredatesRunOwnership`), and nothing fills it in later.
  Compatibility impact: such a project stays readable, listable and archivable but cannot dispatch
  engineering or QA work — 409 from `spawn-worker` and `run-e2e`, a skipped task in the dispatcher,
  a failed story in the deploy supervisor — until it is recreated by a run that names itself.
- Acquisition decides whether a developer worker exists at all; ownership describes a worker that
  does. A developer worker's ownership is therefore stamped by the acquisition of the project's
  workspace lock and nowhere earlier: the stamp goes in, then the `SADD` that takes the project, and
  a worker that loses that `SADD` has its ownership withdrawn before it is refused. Nothing
  describes a worker that was refused, so `project_id` in `worker:meta:<worker_id>` still means what
  every release path reads it as — `delete_worker`, the create-failure cleanup, the stale-lock scan
  in `_check_project_lock`, the workspace GC — and a refused worker releases nothing. The stamp is
  ordered before the `SADD` because it is the evidence the workspace GC reads: a project can never
  be in `workspace:active_projects` with its holder's metadata not yet visible, so that sweep cannot
  take a workspace away from a worker mid-acquisition and let a second creator onto the same
  checkout. The one worker that owns a project without holding its workspace is the QA executor,
  which is excluded from the mutex by its worker type. Two creates that race past the lock check no
  longer both proceed: the `SADD` result decides, and the loser is refused. A refusal is also
  terminal in Redis now (`worker:status` FAILED plus `worker:error`), so the caller — which was
  ACKed before the slow work and then polls status — fails fast instead of waiting out the
  readiness timeout and publishing a delete for a worker that held nothing.

- Worker base images are one immutable release chain keyed by the git SHA. Every green commit on
  `main` builds common and then the claude, codex and factory images from that exact common, and
  publishes all four to GHCR under that commit's SHA, recording the published digests, the SHA and
  the source hash as a run artifact. Nothing publishes a mutable `:latest` any more.
- A release is one registry write, not four. Because four tag pushes cannot be one transaction, a
  SHA is released by a final `worker-base-release:<sha>` marker carrying the digest record of the
  chain, published only once all four images resolve. Image tags left behind by a failed or
  cancelled publish run are inert residue: nothing deploys them, and rerunning the job completes
  that SHA without anybody deleting package versions by hand. Once the marker exists the SHA is
  frozen — a rerun re-verifies the digests it names and pushes nothing.
- The deploy consults the marker first and refuses a revision that has no release (exit 9) before
  any local tag moves, and refuses without repairing a committed release whose image is gone or
  carries the wrong source hash.
- The deploy records the digests it actually verified. The pull half resolves each tag to a digest
  once and then pulls, checks the source hash and retags from `<repository>@sha256:…`, writing that
  record on the host; the deploy copies it back rather than looking the tags up a second time.
- The production deploy pulls the worker images of the revision it deploys by exact release — there
  is no default tag and no fallback — and verifies that each one carries the source hash of the
  checked-out revision. A missing release, a missing image, a missing label or a stale label fails
  the deploy naming the image, the expected hash and the found hash, before `compose up -d` changes
  anything that is running; the deployed SHA and verified digests are recorded in the run summary
  and an artifact.

- Added a production acceptance matrix for the two subscription-backed developer and QA
  executors. The live mega can now select a Claude or Codex developer, forces exploratory QA
  instead of the deterministic health-only shortcut, and verifies the active QA selector. The
  manual production workflow runs all four worker/QA combinations sequentially with owned
  cleanup, then restores the production QA default to Codex.

## 2026-08-12

- Updated developer-worker test guidance to use the generated project's supported
  `make tests` and `make test-integration` targets.

- Preserve cancelled QA outcomes without breaking the deploy dispatch boundary: a cancelled deploy
  without a result may still record its worker's first outcome or be superseded after its lease.

- QA run terminal transitions now record `completed_at` atomically with their
  verdict, including cancellation. Repeated deliveries preserve the first
  timestamp, as do dispatch cancellation and QA-access cleanup failure paths.
- A first QA terminal state is now authoritative even when cancellation has no
  result, and `PATCH /runs/{id}` ignores caller-supplied completion timestamps
  until it records that terminal state itself.

- **Resolve project worker default at the API boundary (`codegen-orchestrator-1177`)**:
  PO project creation no longer injects Claude when the caller omits a developer
  agent. The API now records its current `DEFAULT_AGENT_TYPE` at creation time,
  preserves explicit supported choices, and leaves existing project records unchanged.

- Kept a central QA worker in `STARTING` until worker-manager has installed its agent instruction, `TASK.md` and `/workspace/qa`. The wrapper independently waits for those files before leasing its first turn, closing the creation-to-first-turn race without widening the capability or credential boundary.
- Made Codex the default central exploratory-QA executor. Its intentionally empty ephemeral QA workspace now uses Codex's native `--skip-git-repo-check` mode, while `claude` remains an explicit override and the existing capability-only, isolated-egress execution path is unchanged.

## 2026-08-12 (4)

- Worker bind mounts are prepared in the Docker daemon's mount namespace when the daemon is
  remote. This repairs the manual backend DinD suite without weakening the production worker:
  a short networkless helper gets only the capabilities needed to chown `/workspace` and the
  transcript mount, then exits before the UID 1000 worker starts with its existing dropped
  capabilities and `no-new-privileges` boundary. A local Unix-socket daemon keeps the direct
  host-path preparation path.
- Failure to inject an agent instruction file or `TASK.md` now fails worker creation immediately.
  Previously worker-manager logged `PermissionError`, ACKed the create command and left callers to
  discover the dead worker through a sequence of 60-second timeouts.
- The legacy DinD harness waits for asynchronous worker creation to finish instead of treating the
  early acceptance response as readiness, tolerates cold worker-image builds, and supplies an
  isolated test credential for Factory workers. CLI-presence coverage uses a non-production Claude
  test key rather than pretending the empty DinD session mount contains a subscription session.

## 2026-08-12 (3)

- The deterministic QA probes classify a dependency that did not answer by what
  was unavailable, not by which call happened to meet it first. Two places got
  that wrong.
- Telegram rate limiting a `getMe` is no longer read as a dead bot. Only HTTP
  401 and 404 are the Bot API refusing this token, and only those are
  `bot_not_live`; HTTP 429 and every other non-OK answer are Telegram declining
  to answer, which establishes nothing about the bot. They travel the
  infrastructure route instead: bounded retries, `qa_probe_unavailable`, one
  administrator alert. When Telegram sends `parameters.retry_after` it comes
  back on `BotLiveness.retry_after` and the probe waits that long rather than
  its own guess — up to `BOT_LIVENESS_MAX_RETRY_DELAY`, past which it stops and
  reports the same outcome naming the window Telegram asked for, so the budget
  stays bounded. Before this, a flood-controlled request blocked the run for a
  human as though the token had been revoked.
- Docker not answering on the target now ends at the same outcome from both
  calls that read it. The `docker ps` in `resolve_capabilities` runs before a
  session exists and used to raise a bare `QACapabilityError`, which
  `run_qa_centrally` merged with a failed SSH grant into `server_unavailable` —
  no retries, and no administrator alert, for exactly the condition the later
  `docker inspect` retries and alerts on. It raises `QAContainerRuntimeError`
  now, retries `CONTAINER_PROBE_ATTEMPTS` times like every other read of that
  runtime, and is classified by `container_runtime_unavailable()`, the one
  function both paths come to. `server_unavailable` keeps its meaning: the run
  never got onto the host, or the deployment directory does not resolve — cases
  in which no docker call was made at all.

## 2026-08-12 (2)

- Container state and "is the bot alive" are established deterministically now,
  before the exploratory QA executor starts. Both were the odd ones out among
  the five facts QA needs: container state was reachable only through the
  agent's own `container_inspect` tool, so a dead container cost a model call to
  discover, and bot liveness at QA time was not checked at all — the only `getMe`
  in the system ran when the token was bound, possibly weeks earlier, and deploy
  smoke's runs at deploy time.
- `run_container_state_checks` reads `docker inspect` for every container of the
  run's compose project over the session the run already holds — no new access
  path, the same call the agent had. A container that is exited, restarting or
  unhealthy fails QA as a product defect with one failed check naming it, and no
  executor is started for it.
- Bot liveness is asked of the API, which holds the token: `GET
  /api/projects/{id}/telegram/liveness` (internal or admin) calls `getMe` and
  answers `BotLiveness` — a state, the username Telegram reported, a detail
  line. The token enters neither the QA runtime nor the deploy target, which is
  the whole reason the question is asked this way rather than by lending the
  credential to QA. A bot Telegram refuses is a blocker for a human, not a fix
  task: no engineering worker can re-issue a revoked token.
- The infrastructure half reuses what was already there rather than growing a
  second mechanism. `QAInfrastructureFailure` now carries the blocker it becomes,
  so a probe that could not be performed travels the same path a missing
  executor does: bounded retries, a typed QA-infrastructure outcome, and one
  administrator alert. `QA_INFRASTRUCTURE_BLOCKERS` in the consumer is the list;
  `_alert_admins_no_executor` became `_alert_admins_qa_infrastructure` and names
  the category it is alerting about.
- Two blocker categories are added because the existing ones would have made the
  repair ambiguous. `qa_probe_unavailable` is "we are on the host, or on our own
  API, and what we asked did not answer" — distinct from `server_unavailable`,
  which is never having got onto the host. `bot_not_live` is Telegram refusing
  the stored token — distinct from `telegram_access_denied`, a live bot refusing
  the QA account, which the temporary-access mechanism repairs.
- What the probes established is handed to the executor as given, so the run is
  not spent asking again: `build_qa_prompt` takes `established_facts`, states
  them under "Already established", and replaces the container line in the
  checklist with one saying not to re-check it. The tool set, the read-only
  rules and the result JSON are unchanged, and `QAResult` gains no field —
  `services/langgraph/tests/unit/test_qa_runner.py` pins that the only line
  which leaves the prompt is the checklist item now answered.

## 2026-08-12 (1)

- A terminal story outcome can no longer be observed without the owner's message
  being either published or durably owed. The supervisor committed the
  transition and then published to `po:input` — an `xadd` with nothing behind it
  — and the commit is precisely what takes the story out of `TESTING`, the only
  status that loop scans. A transient failure of the publish, or of the
  recipient lookup in front of it, lost the message permanently: the owner's
  product was finished, deployed and verified, and nobody ever told them. Worse
  in practice, the exception escaped `supervise_testing_stories` and ended the
  rest of the dispatcher tick with it.
- `shared/contracts/dto/owner_notification.py` is the record that closes the
  gap, written into the run's `run_metadata` under `owner_notification`
  *before* the transition is committed — the same shape the QA SSH grant already
  uses for access, and for the same reason. `OWED` is work; `DELIVERED` is the
  stream having accepted the event; `UNADDRESSABLE` is a recipient with no
  Telegram chat, which is an answer rather than a failure and is never retried;
  `ABANDONED` is three transient failures, after which an admin alert names the
  event, story, project and run.
- Because the record is written first, it is not evidence that the transition
  it was written for happened, and it is not read as such: it carries the
  `terminal_status` that transition produces, and nothing is published until the
  story is read back and found in it. Without that check the seam would have
  traded a lost message for a false one — a record committed on a run whose
  story transition then failed would tell the owner their product is finished
  while it is still in testing, and a story that never transitioned would be
  announced as complete. A record whose transition is not there is `VOIDED`:
  nothing is published, no attempt is spent, and the obligation is written again
  from scratch if routing later does reach that ending. The same check covers
  the opposite failure for free — a transition that committed and lost its
  response leaves the story terminal, so its message is delivered.
- `services/scheduler/src/tasks/owner_notifications.py` is the single seam, and
  all three terminal paths in `supervise_testing_stories` go through it: QA
  passed, an unverifiable application quarantined, and QA fix attempts
  exhausted. The last of those told administrators only; the owner now hears
  about it too, under the `story_quarantined` event PO already routes, because
  that transition ends the story for its owner exactly as a quarantine does. The
  admin alert on that path is unchanged.
- The supervisor's two remaining terminal owner notifications take the same
  seam, and both used to publish behind `except Exception: log.warning` — the
  same loss, with the failure recorded as a warning.
  `_escalate_refused_deploy` with `tell_owner` (`story_impossible_capacity`)
  parks the story for a human, and `_park_task_waiting_resources` on
  `HUMAN_REVIEW_WITH_OWNER_NOTICE` (`task_impossible_capacity`) parks a failed
  engineering task *and its parent story* for one. The second is about the task,
  so the record keeps the task id and PO is still told which task it is; the
  record itself lives on the engineering run. A refusal escalated with
  `tell_owner=False` is still admin-only and owes nothing.
- The four publishes left in `supervisor.py` are the non-terminal ones —
  `task_waiting_resources`, `task_waiting_infrastructure`,
  `task_resources_resumed` and `story_waiting_user_secret`. They stay direct and
  they stay best effort, outside this guarantee: no scan re-derives them.
  `_notify_resources_resumed_via_po` fires once on the `backlog → todo` move and
  never again for a task in `todo`, the first wait messages are published only
  under `is_new_wait`, and the `waiting_user_secret` scan redispatches the story
  once the secret arrives rather than re-sending the request for it. A publish
  lost there means the owner does not get that message at all, and the bounded
  retry and admin alert here do not extend to it.
- `supervise_owed_owner_notifications` re-attempts what a committed transition
  still owes, reading its work from the new `GET /api/runs/owner-notifications/owed`
  — selected by the state of the record, ordered oldest first, bounded by a page,
  with age deliberately not a selection key so an outage cannot put a message out
  of reach. It runs before story routing in the cycle, so a record owed by this
  tick gets exactly the one in-tick attempt routing makes. `supervisor_cycle`
  gained `owner_notify_recovered`, `owner_notify_retrying`,
  `owner_notify_exhausted`, `owner_notify_unaddressable` and
  `owner_notify_voided`, which is what tells "still being chased" apart from
  "given up on and handed to a human" apart from "there was nothing to say".
- Delivery is at-least-once and bounded to the publish leg. A process that dies
  between the publish landing and the record being marked delivered republishes;
  what is guaranteed is that a settled record is never published twice and an
  owed one is never forgotten. The transport leg to Telegram keeps its own
  bounded retry and admin alert in `services/telegram_bot/src/proactive.py`.
- Regressions:
  `services/scheduler/tests/unit/test_owner_notification_durability.py` drives
  the real ordering — transition committed, publish refused, next cycle delivers
  — against a store that keeps what the API would keep, along with both ways the
  two requests come apart (a transition that never committed publishes nothing
  and is voided; one that committed and lost its answer delivers exactly once)
  and the impossible-placement task notice taking the same route, and
  `services/api/tests/service/test_owner_notification_selection.py` holds the
  selection: a month-old owed record is still work, a delivered or settled one is
  not, and the page bounds the answer.

## 2026-08-11 (16)

- The QA executor's CLI can reach its model backend again. worker-manager put
  the run's egress proxy into the container environment, but the wrapper starts
  the agent with an explicit replacement environment whose allowlist did not
  name `HTTPS_PROXY`, `https_proxy`, `NO_PROXY` or `no_proxy` — so the child
  process, on a container attached to exactly one internal network, had no
  address for its backend at all and every run ended as
  `qa_executor_unavailable`. `QA_EGRESS_PROXY_ENV` in
  `packages/worker-wrapper/src/worker_wrapper/wrapper.py` now passes exactly
  those four to a QA executor's agent and nothing else; a developer agent, which
  has an ordinary network and no proxy, is unchanged. The boundary is not
  weakened: it is the same allowlisted CONNECT-only proxy, and the internal
  network is still the thing that holds it.
- The regression test is at the boundary the defect lived on — the environment
  `create_subprocess_exec` is actually called with, not the container's — and
  `services/worker-manager/tests/unit/test_qa_egress.py` now asserts that the
  variables worker-manager sets are exactly the ones the wrapper forwards, so
  the two lists cannot drift apart again.
- Workers created before this branch survive the rollout. Both control-plane
  boundaries now decide from a recorded `worker_type`, and records written by
  the previous release have none — a developer worker still running when the
  control plane is replaced would have lost its lease, status, session, result
  and Compose routes mid-turn, because worker containers and their Redis state
  are not Compose services. `shared/worker_type_cutover.py` marks those records
  `developer` once, at startup, in the broker (`worker:broker:*`) and in
  worker-manager (`worker:meta:*`). It is a proof and not a guess: the QA
  executor and the recorded type arrive in the same change, so a typeless record
  cannot be a QA worker. The request path keeps no fallback — a typeless record
  appearing later is still refused everything — and the migration is due for
  deletion once no pre-cutover worker can exist, since these records die with
  their worker.
- `services/worker-manager/tests/service/test_control_plane_rollout.py` is the
  rollout regression: a pre-cutover record written into the real Redis is
  refused before the restart, the real broker and worker-manager containers are
  restarted the way a deploy restarts them, and afterwards the old credential
  runs its whole turn over real HTTP and keeps Compose at both hops while the QA
  credential is still refused at both. Both services are recreated by the same
  `docker compose up -d` in `docs/DEPLOY.md` and the deploy workflow; DEPLOY.md
  now says so explicitly, because rolling out the broker alone would make an old
  worker-manager's registrations fail.

## 2026-08-11 (15)

- A QA executor now has no control-plane authority beyond the protocol of its
  own turn. Its broker token cannot be hidden from the agent — the CLI runs as
  the same user as the wrapper that holds it, so `/proc/<ppid>/environ` gives it
  up — so the token itself was worth a `POST /v1/workers/{id}/infra/compose`,
  and `docker compose build` of an agent-written Dockerfile executes arbitrary
  `RUN` instructions on the management host's builder, outside the QA
  executor's internal network and its proxy.
- The refusal is an allowlist per worker type, not a patch on one endpoint:
  `shared/contracts/worker_control_plane.py` names every operation a worker
  credential can ask for and grants a `qa` worker the turn protocol
  (`input.lease`, `output.submit`, `status.update`, `session` read/write/clear)
  and nothing else. Adding an operation to the enum without classifying it as
  turn-protocol or Docker-daemon fails a test, so a future route is refused to
  QA until someone decides otherwise.
- It is enforced at both boundaries that already duplicate the token check —
  `services/worker-broker/src/main.py` (every worker route now states the
  operation it authorizes, so a new route cannot inherit permissions silently)
  and `services/worker-manager/src/routers/compose.py`, which is reachable
  directly with the same token. Both read the worker type from a server-side
  record written before the credential existed (`worker:broker:{id}` at
  registration, `worker:meta:{id}` at creation); nothing in the request says
  what kind of worker is calling, and an unrecorded type is refused. Developer
  workers are unchanged and keep every operation.
- `services/worker-manager/tests/service/test_qa_control_plane_boundary.py` is
  the end-to-end regression against a real broker, a real worker-manager and a
  real Docker daemon: a developer worker's token really does cause a host-side
  build (the image exists and carries a marker only a `RUN` on that daemon could
  write), while a QA worker with the identical workspace and request is refused
  by both boundaries, produces no image and no compose plan, and still runs its
  own turn. Worker-manager's unit doubles moved to `tests/unit/conftest.py`, so
  a service test can no longer be handed a mocked broker registration.

## 2026-08-11 (14)

- "Exploratory QA cannot write to the application" is now a property of the
  executor's network instead of a rule in its prompt. The QA executor container
  is attached to `codegen_qa_egress` — declared `internal: true` — and to
  nothing else, so the deployment's public URL, the fleet and the internet are
  unreachable from it rather than forbidden to it. Reachable on that network are
  the run's capability endpoint (`qa-worker`), the worker broker, and one
  per-run egress proxy. The public URL stays reachable only through the typed,
  GET-only `http_get` the runtime performs. Developer workers are untouched:
  they keep `codegen_worker` and its ordinary connectivity.
- The proxy (`services/worker-manager/src/qa_egress_proxy.py`) speaks HTTP
  `CONNECT` and nothing else, to the assigned CLI's model backend and nothing
  else (`QA_CLAUDE_BACKEND_HOSTS` / `QA_CODEX_BACKEND_HOSTS`, defaulting per
  agent). It cannot be used as a forward proxy, so it cannot carry a `POST`, and
  a `CONNECT` to the deployment is refused with `403` by the same code that
  refuses any other host. It is created with the run and removed with it,
  including by orphan GC.
- Fail-closed, in `services/worker-manager/src/qa_egress.py`: worker-manager
  proves the network is internal before anything is created, proves the proxy is
  listening before the executor exists, and proves the started container is
  attached to that single network. Any of those failing fails worker creation,
  which the QA runtime already turns into the typed `qa_executor_unavailable`
  QA-infrastructure outcome — never a silent start with an unrestricted
  container and never a product defect.
- The runner's transcript/tool-trace write scan is unchanged and is now a second
  layer over an enforced boundary rather than the boundary itself.
- `services/worker-manager/tests/service/test_qa_egress_boundary.py` proves it
  against a real Docker daemon: a recording application, a real executor
  container built by the production policy, `POST`/`PUT`/`PATCH`/`DELETE` from
  `curl` and from a Python client with the proxy variables stripped, and zero
  write requests in the application's ledger — with positive controls that the
  ledger records, that the capability endpoint answers, and that an allowlisted
  tunnel carries a request and a response.

## 2026-08-11 (13)

- Exploratory QA is performed by the assigned subscription coding agent again —
  Claude Code by default, Codex when `QA_EXECUTOR_AGENT_TYPE` says so — started
  centrally on the management host through the existing worker runtime. There is
  no second mechanism for starting agents: `clients/qa_worker.py` sends the same
  `worker:commands` create/status/delete a developer worker is started with, and
  asks for a `qa` worker, which has no repository, no git credentials, an empty
  scratch workspace deleted with the container, and one injected command.
- That command (`shared/qa_probe_cli.py`, installed at `/workspace/qa`) is the
  container's only route to the deployment. It posts named calls to a per-run
  capability endpoint served by `qa-worker`
  (`agents/qa/capability_service.py`), which dispatches into exactly the tool
  set the in-process agent used — `agents/qa/tools.build_qa_callables`, now the
  single boundary behind both front-ends. The SSH identity, the fleet key and
  the Telegram session stay in `qa-worker`; the container holds a URL and a
  token that stop working when the run ends.
- `QA_LLM_*` is an optional API fallback, read only after the assigned executor
  has actually failed to run, and never at startup or at the beginning of a run.
  Empty values are a supported production configuration. A transient executor
  failure is retried once (`QA_EXECUTOR_ATTEMPTS`); a missing or broken session
  is not retried.
- With no executor and no complete fallback, the run ends as
  `qa_executor_unavailable` with an administrator alert through
  `notify_admins_best_effort` carrying story, project, run and what is missing.
  `QABlockerCategory.CLAUDE_UNAVAILABLE` is removed: it had come to mean only
  "no LLM API key", which stopped being true.
- The write guard now also scans what the executor's container reported, since
  that container has a shell. `qa-worker` joins the `codegen_worker` network so
  the endpoint is reachable from the executor; see `docs/DEPLOY.md` for what the
  container can and cannot reach, path by path.
- Exploratory QA runs on `claude` or `codex` and on nothing else, enforced in
  both places the executor is named: `QA_EXECUTOR_AGENT_TYPE` fails validation
  when the service reads its configuration, and a `qa` create command carrying
  another agent is refused by the `WorkerConfig` contract worker-manager
  validates every command against, before a container exists. `factory` would
  run QA on a provider API key and `noop` performs no testing at all. Developer
  workers keep the full `AgentType`.

## 2026-08-11 (12)

- Delivering the product no longer waits for the temporary QA access to be
  handed back. `supervise_testing_stories` used to skip any story whose QA run
  still held a live grant, so a story that deploy, smoke and QA had all passed
  stayed in TESTING for as long as the revoke kept being retried — and the user
  heard nothing at all in the meantime. It now routes on the QA outcome and
  nothing else: a passed run completes its story on the next supervisor tick.
- The owner is told in the same tick. A `story_completed` event goes to
  `po:input` with the address of the deployment QA tested — URL, and the bot's
  `@username` when there is one — read off the handoff stored on the QA run.
  Nothing published that event before; the QA consumer stopped sending it when it
  was decoupled from the story lifecycle, and the supervisor never picked it up.
- The cleanup is unchanged and still finishes on its own: the same sweep revokes,
  reads the running service back, retries within its bounds, and when they are
  spent writes the `qa_cleanup_failed` blocker on the QA run and alerts an
  administrator — now naming the story, project, QA run and grant, so the
  incident can be picked up from the message alone.
- A leftover test identity is a cleanup incident, not a failed product. A
  completed story is never reopened by anything the grant does afterwards
  (only TESTING stories are routed at all), and story routing now runs *before*
  the access sweep in the dispatcher cycle, so a cleanup that ran out of attempts
  during an outage cannot write its incident onto a QA run before the story
  behind it has been routed.
- Being stuck is visible without a dashboard: the sweep's counts separate
  `revoke_failed` (an attempt that will be retried) from `escalated` (given up
  on, a human called), and both are on the `supervisor_cycle` log line.
  `qa_waiting_for_access` is gone with the wait it counted.

## 2026-08-11 (11)

- An admission refusal is no longer told as a memory shortage. A live acceptance
  run placed work while its only managed host was still provisioning: the
  allocator refused correctly, then named the reason `insufficient_free_memory`
  on an empty 4 GB machine. The search path only kept its own reason for the two
  provisioning rejections, so a host in a non-admitting status — or one that
  stopped being managed — fell through to the last line of the search.
- The reason a refusal carries now lives beside the rejections themselves, as
  `shared/server_admission.py::ADMISSION_FAILURE_REASON`. It is one constant, not
  a rejection-to-reason table: no admission rejection is a statement about how
  much memory was asked for, so there is nothing to branch on. Both placement
  paths — the search for a new host and the re-admission of a bound one — raise
  it, and `test_both_placement_paths_refuse_with_the_same_reason` compares the two
  paths against each other for every refusing state, so the drift that happened
  here is not expressible again.
- No new vocabulary member: `SERVER_NOT_PROVISIONED` still describes it, so its
  consumers (`shared/allocation_disposition.py`, the supervisor's PO event
  choice) are untouched and the disposition stays `INFRASTRUCTURE_WAIT` — a
  bounded wait that ends with a human, never a message to the owner about
  capacity and never a failed story. Capacity reasons stay reachable only for
  hosts that passed admission and then ran out of room.
- One thing still outranks the admission reason in the search path, and now says
  so out loud: a request no managed server could fit even fully admitted stays
  `IMPOSSIBLE_CAPACITY` with `OPERATOR_REVIEW`. Waiting out provisioning does not
  make a small host bigger, so an infrastructure wait there would park the request
  on an event that never arrives, while an operator can be told at once that the
  fleet has no machine of the required size. That is not a host's state retold as
  a memory shortage; it is a separate durable fact. The order is now a property of
  the code — a comment at the check, a pair of tests one fixture apart that draw
  the line between "not ready yet" and "would never fit", and a cross-path test
  naming the one question only the search can ask.

## 2026-08-11 (10)

- The QA identity is now proved on the target before anything records that a
  host has one. An account named `qa-observer` that this role did not create can
  carry `uid 0`, a rule in somebody else's `/etc/sudoers.d` file or an ACL on the
  docker socket, and none of the role's tasks took those away — so ownership is
  established first, by a root-owned marker the role itself writes
  (`/etc/codegen-qa-identity/qa-observer`), and an account found without it is
  refused rather than adopted or repaired. Nobody else's sudoers file is deleted:
  that is an administrator's policy, and losing it silently would be worse than
  stopping.
- After the account is configured, the role asks the machine what came of it
  (`roles/qa_identity/files/qa-identity-proof`): `uid != 0`, no privileged group,
  everything `sudo -l -U` grants is exactly the one wrapper rule, and the account
  itself cannot open `/var/run/docker.sock` — which answers group, mode and ACL
  together. A failed proof fails the phase, so the label is never written and the
  host keeps refusing QA, visibly.
- A target that lost the account after a successful provisioning is journalled
  like one that never had it. The install script already separated "no such
  account / no `authorized_keys`" from every other failure; that now reaches the
  provisioning journal as `qa_identity_absent_on_target` against the server
  handle, with the retrofit command. Other central-QA failures are deliberately
  not provisioning facts and stay out of it.
- A retrofit the role refuses is recorded as a `provisioning_failed` incident
  against the handle instead of only failing the command, and the per-host report
  now names, for each thing left in place, why it stayed and the command that
  removes it — including `/swapfile`, which this playbook still does not touch
  because the old runner's swap and an administrator's own are the same file.

## 2026-08-11 (9)

- Gave a QA run an identity that provisioning creates. The `qa_identity` role
  makes `qa-observer` on every target, from the same phase that records
  `provisioning_phase=complete`, and that completion write now records
  `labels.qa_ssh_user` in the same call — so a host cannot read as provisioned
  and lend no account. The account's primary group is its own, stated explicitly,
  and its supplementary list is exactly empty, so neither membership can be
  `docker` (which is root on the host) even on a host where somebody had already
  made the account inside it. It has one sudo rule, and reads the deployment tree
  through an ACL entry rather than by joining the group that can write it.
- The QA runtime trusts `labels.qa_ssh_user` for *whether* a host was
  provisioned, not for *whose* `authorized_keys` to write into. `servers.labels`
  is an untyped dict the server API will PATCH, so only the name provisioning
  itself writes is accepted; any other value is refused as
  `qa_identity_not_attested` before anything connects to the target. Editing a
  server row is not a way to point a QA run at an existing privileged account.
- Moved the "what may this account do with docker" boundary onto the target.
  `/usr/local/bin/qa-docker` allows `diff, inspect, logs, port, ps, stats, top`
  and refuses `exec`, `run`, `cp`, `build`, `commit` and the rest before docker
  is reached; the QA account may run that command and nothing else. Which
  containers a run may name is still the run's capability set, in the
  orchestrator. The runtime's docker calls go through `sudo -n qa-docker`.
- The QA runtime takes the run's identity from the server row, not from
  `ssh_user`. The fleet key and the administrative account are used only to
  append the run's one-shot key to `qa-observer`'s `authorized_keys` and to
  remove it; the run itself connects as `qa-observer`. The runtime creates no
  account and no file — a target missing either refuses the install — and
  `QASshGrant` now records both accounts, because the sweep has to connect as
  one and clean the other's file.
- A fresh host provisioned the ordinary way now passes exploratory QA. Before
  this, a server row created by `server_sync` (no `ssh_user`, so `root`) was
  refused with `server_unavailable`, which was the accepted cost of moving QA
  into the orchestrator.
- A target with no QA account is still refused, but visibly: the refusal is
  journalled as a `provisioning_failed` incident against that `server_handle`
  with `details.step = qa_identity`, so it reaches an administrator through the
  existing mechanism and the host stops taking new applications until repaired.
- Added the retrofit for hosts provisioned before this:
  `python -m src.provisioner.qa_identity_retrofit <handle>` in infra-service. It
  creates the same identity from the same role and removes only what is
  positively the old runner's: `~/.local/bin/claude`,
  `~/.claude/.credentials.json`, `~/.qa-telethon.env` and `/opt/qa-runner`. The
  cleanup runs in the administrative account's home, which is also a person's
  home, so `~/.claude`, `~/.local/share/claude` and `/swapfile` are left alone —
  the CLI directories are equally an administrator's own, and `/swapfile` cannot
  be told from swap somebody else made, where removing 2GB from a live host is an
  outage rather than cleanup. The label is written only after the playbook
  succeeds, and the run reports per host both what it removed and what it left.
- `docs/DEPLOY.md` said "servers provisioned by the current Ansible have a deploy
  user and are unaffected". They did not: the role configured the account named
  by `deploy_user`, which was `root`, and put it in the `docker` group.

## 2026-08-11 (8)

- Made the QA grant sweep's walk survive the selection it is draining. The pages
  were taken by `offset` over a selection that shrinks while it is walked: a
  successful revoke writes `RELEASED` and the row leaves the predicate, so the
  records still open slide backwards past the cursor. With a whole first page
  released, `offset=100` lands past the end of what is left, the response comes
  back short, and the cycle stops with an unreconciled grant — a live
  `authorized_keys` line — that it claimed to have walked to.
- `GET /api/runs/qa-ssh-grants/held` now pages by cursor: `after_created_at` and
  `after_id` name the last record handled, the next page is strictly after it in
  the `(created_at, id)` order, and half a cursor is a `422` rather than a
  silent restart from the top. `offset` is gone from the route and the client
  rather than kept as a second mode. A position in the order cannot be moved by
  rows closing behind it, so one cycle presents every record that was open when
  it passed.

## 2026-08-11 (7)

- Stopped selecting the QA grant sweep's work by time. It read QA runs started
  in the last 24 hours, so an outage longer than the window put an unreleased
  record permanently out of reach: no revoke, no readback, no `qa_cleanup_failed`
  escalation, and the `authorized_keys` line it stands for left on the target
  for good. The window was the wrong key — a record is work while it is
  unreleased, whether that became true a minute ago or a month ago — and
  `GRANT_SWEEP_LOOKBACK` is gone rather than widened.
- Added `GET /api/runs/qa-ssh-grants/held` (internal/admin) so the selection can
  be made on the record: every run whose `qa_ssh_grant` is not `released`,
  oldest first, `limit`/`offset`. The page bounds the response and not the
  coverage — `sweep_qa_ssh_grants` walks pages until one comes back short, so
  nothing is dropped for being past the end of one.
- Kept an unparsable record visible instead of hiding or crashing on it.
  Unreadable is not released, so it is still selected; the sweep counts it,
  logs `qa_grant_sweep_unreadable_record` and continues, because ending the
  cycle on it would make every record behind it unreachable — the same failure
  from the other side.

## 2026-08-11 (6)

- Gave a central QA run one explicit capability set and made every tool derive
  its boundary from it. Before, each tool invented its own rule and three of
  them were wrong on a shared host: `docker ps`/`images`/`stats` listed the
  machine, `localhost_http_get` accepted any port in 1..65535, and path
  containment was lexical, so a symlink in the deployed tree read a neighbour's
  `.env` while still looking "inside". The set is resolved once per run from
  deployment data — physical root via `readlink -f` on the target, containers
  via the compose project label docker itself stamps, the application's
  allocated ports, the public URL — and a tool whose boundary cannot come from
  it is gone rather than patched.
- Removed the host-wide command surface for that reason. `remote_exec` is now
  read-only docker sub-commands (`diff`, `inspect`, `logs`, `port`, `stats`,
  `top`) that must name a container in the set; `docker ps`, `docker images`,
  `df`, `uptime` and `journalctl` describe the machine and no capability can
  bound them.
- Made path containment physical: the read resolves on the target and checks
  membership of the physical root after resolution, in the same command, so a
  symlink cannot widen it and a separate resolve cannot answer about a path the
  read no longer uses. The secret-name check stays on top of that, not instead
  of it.
- Made the fact of a target grant durable. `QASshGrant`
  (`shared/contracts/dto/qa_ssh_grant.py`) is written to the QA run's
  `run_metadata` **before** the key install is attempted, so an append that
  lands while its answer is lost still leaves a record; `RELEASED` is written
  only after the target is read back. `sweep_qa_ssh_grants` in `qa-worker`
  reconciles every unreleased record, and after three failed attempts writes the
  run's outcome as a `qa_cleanup_failed` blocker. Residual access now also
  reaches the run's result on the early-return path where the install itself
  failed, which previously reported only `server_unavailable`.
- Stopped the revoke from being able to empty `authorized_keys`. It rewrites the
  file the fleet key itself is authorized by, and the old form copied the filter
  result over it unconditionally — a filter that came back empty would have
  taken the orchestrator's own line with it and locked the target out for good.
  It now refuses to install an empty result, which leaves the marker readable
  and hands the grant to the sweep instead of closing it.
- Refused exploratory QA on a target whose run identity would be root. AC4 asked
  for an unprivileged identity and `ServerCreate.ssh_user` defaults to `root`,
  which made the two impossible to satisfy at once on such a host; the run is
  now blocked as `server_unavailable` rather than performed privileged. Health-
  only criteria are unaffected — they never SSH — and servers provisioned by the
  current Ansible have a deploy user.

## 2026-08-11 (5)

- Moved exploratory QA off the deploy target. It used to be a Claude Code CLI
  living on the tested server, driven over SSH with the fleet's own server key,
  fed OAuth credentials the runner pushed and refreshed there, and a Telethon
  session Ansible wrote into the deploy user's home. QA is now a ReactAgent in
  `qa-worker`: the LLM and the QA Telegram account are the orchestrator's, and a
  clean target — no `claude` in PATH, no LLM credentials, no Telethon session —
  passes a full exploratory run.
- Replaced the agent's shell with a closed set of typed tools bound to one run
  (`services/langgraph/src/agents/qa/tools.py`): public GET, loopback GET, a file
  read scoped to the deployment directory, an allowlisted read-only command,
  container logs/inspect, and a Telegram probe. Every path, container and port is
  checked against the one `QATarget` the run owns, so naming another deployment
  is refused rather than answered.
- Made the identity one-shot. Each run mints an ed25519 key, installs it in the
  target's `authorized_keys` with `restrict` and an `expiry-time` using the fleet
  key — held by the runner, never by the agent — and removes it in `finally` on
  every path out, then reads the file back to prove it is gone. The run also gets
  an isolated central workspace, destroyed the same way. Anything that survives
  becomes a `qa_cleanup_failed` blocker instead of a green run.
- Kept the write guard by removing what it guarded. The old guarantee was a
  Claude `PreToolUse` hook filtering Bash command lines for writes to the
  application; there is no Bash now, and no tool takes an HTTP method, so a write
  is inexpressible. The runner-owned trace of every tool call is still scanned
  with the same `_forbidden_application_write`, and a write found in any evidence
  the runner owns still quarantines the run with a residual state trace.
- Dropped the `qa_runner` Ansible role from provisioning, along with the 2GB swap
  it needed to unpack the CLI, the copied `.credentials.json`, the
  `/opt/qa-runner` venv and `~/.qa-telethon.env`. New servers get none of it.
  Servers provisioned earlier still carry it; nothing reads it, and removing it
  is a separate task (see `docs/DEPLOY.md`, "QA runtime (central)").
- Removed `credential_refresh_loop`, which kept a Claude OAuth token alive on
  every managed server. There is no token out there to refresh.
- Added the `qa` LLM env group (`QA_LLM_MODEL` / `QA_LLM_BASE_URL` /
  `QA_LLM_API_KEY`). Missing config blocks exploratory QA with the existing
  `claude_unavailable` category rather than producing a verdict; health-only
  criteria still run over HTTP and spend no LLM. `QAResult`, `QABlocker` and
  `QABlockerCategory` are unchanged, and the prompt and result parsing keep their
  meaning.

## 2026-08-11 (4)

- Closed the last way past admission: reuse. A project already bound to a server
  skipped the rule entirely — `ensure_project_allocations()` fetched the bound
  host, read no incidents, and handed back its allocations or took a fresh port
  on it for a newly declared module. So a redeploy landed on a host whose
  provisioning had restarted or broken, which is the placement the rule exists to
  refuse. The bound host now passes the same `shared/server_admission.py`
  predicate over the same snapshot of active incidents, before any allocation is
  returned and before any port is taken.
- Refused reuse through the existing typed `AllocationError` with the admission
  budget, so it travels the route every other refusal travels — a bounded
  infrastructure wait — instead of reaching the deploy path as a `GIVE_UP` that
  would fail the user's story. No new contract, reason or outcome.
- Bounded the resume-and-refuse cycle that reuse makes reachable: resuming asks
  whether any server is admissible, while a bound project is refused by the one
  it sits on, so a fleet with one healthy host and one broken host the project is
  pinned to would re-dispatch and be refused forever. The deploy wait now checks
  `supervisor.resource_wait_timeout_minutes` before admissibility, as the
  engineering wait already did, and the story reaches a human.
- Extended the shared admission matrix to the reuse shapes — existing
  allocations returned whole, and a new module taking a port on the bound host —
  so all placement paths are checked against the same state table.

## 2026-08-11 (3)

- Gave every refusal disposition its own behaviour on both routing paths. The
  deploy path answered all of them with one infrastructure wait, so a request
  larger than any managed server — classified `OPERATOR_REVIEW` precisely
  because waiting is pointless — sat in DEPLOYING being re-polled forever with
  no human told and no way out. `shared/allocation_disposition.py` now carries
  `REFUSAL_ROUTING`, a disposition × path table with exactly one behaviour per
  cell and no behaviour repeated within a path, and both routers branch on it:
  `_route_refused_deploy` in the scheduler's deploy routing and
  `_park_task_waiting_resources` on the engineering side. A disposition that
  starts routing like its neighbour now fails a suite.
- Routed `OPERATOR_REVIEW` to the human-review queue on the deploy path, the
  same queue a quarantined QA story reaches and entered the same way: the reason
  is recorded on the story, the `human-review` action moves it, operators are
  alerted, and the owner is told the request needs an operator rather than being
  left watching a wait. `StoryStatus.DEPLOYING → WAITING_HUMAN_REVIEW` is now a
  valid transition, which is what that route needed; `fail_story` remains out of
  reach for every infrastructure disposition.
- Named `TECHNICAL_FAILURE`'s behaviour instead of leaving it a leftover: a fleet
  the platform cannot see is escalated to a human with an operator alert and no
  message to the owner, on both paths. It does not wait (the wait's own re-check
  needs the missing metrics) and no longer spends engineering iterations on a run
  the allocator will refuse at the same point.
- Bounded the deploy infrastructure wait with
  `supervisor.resource_wait_timeout_minutes`, the bound the engineering wait
  already had, and carried the wait's start across re-dispatches in
  `run_metadata`. A refused deploy with no `head_sha` — which no wait can supply
  — goes to a human immediately instead of polling forever.
- Fixed story escalations that reached nobody: the supervisor posted
  `waiting_human_review` as a story transition, which is a status value and not
  a route, so the API answered 404. Every escalation now uses the `human-review`
  action endpoint.

## 2026-08-11 (2)

- Stopped an allocation refusal from terminating a user's story on the deploy
  path. A deploy that could not be placed used to have its typed reason
  flattened into an error string and recorded as `GIVE_UP`, which the scheduler
  turns into a failed story and a product-failure alert — so an unfinished host
  build reached the owner as a broken project. The classification now survives
  the boundary: `DeployOutcome.WAITING_INFRASTRUCTURE` carries the
  `AllocationFailureReason` and the admission budget the attempt asked for, and
  the contract refuses that outcome without them. The story stays DEPLOYING and
  the deploy is re-dispatched once the shared admission rule accepts a target
  again.
- Put the rule that decides this in one place, `shared/allocation_disposition.py`.
  Every allocation reason is classified there explicitly as infrastructure —
  a wait, an operator review, or a technical failure — and the precedence is
  stated once: an allocation refusal outranks a product failure seen in the same
  attempt. Both routing paths call it and neither keeps a reason list of its own,
  so the engineering wait and the deploy wait cannot drift apart the way they
  just did. `shared/tests/allocation_routing_cases.py` pins the wire shape both
  sides agree on.

## 2026-08-11

- Made a server's provisioning state part of admission, so a project application
  can no longer be placed on a host that has not finished (or has failed) its
  software installation. `provisioning_phase` — written by the provisioner into
  `servers.labels` and until now read by nobody — is required to be `complete`,
  and a server carrying an active `PROVISIONING_FAILED` incident is refused even
  when it is. The rule is fail-closed: a missing, empty or unknown phase counts
  as unfinished, because an unknown provisioning state is not readiness. It
  lives once, in `shared/server_admission.py`, and both decision points now call
  it — the allocator that picks the host (`_find_suitable_server`) and the
  scheduler rule that lets a capacity-parked task resume (`_resources_available`)
  — so "resources became available" can no longer mean something different from
  "this server may take an application". One shared state matrix
  (`shared/tests/server_admission_cases.py`) is asserted against the predicate
  and both paths, so a future divergence fails a suite.
- Kept an unfinished host build an infrastructure situation rather than a
  product defect. Admission that fails for this reason raises the new
  `AllocationFailureReason.SERVER_NOT_PROVISIONED` instead of collapsing into a
  memory shortage; the task parks in `waiting_resources` on the existing wait
  path — no engineering retry, no story failure, no admin product-failure alert
  — and the owner is told through a new `task_waiting_infrastructure` PO event
  that says the machine is still being prepared, never that capacity ran out.

## 2026-08-10

- Separated the internal user id from the Telegram chat id in every queue and PO
  contract. `user_id` — which meant a Telegram id from the bot and a `User.id`
  from the scheduler — is gone; messages now carry `telegram_chat_id` (the
  destination) and, where it matters, `owner_user_id` (identification only).
  Scheduler and API producers resolve `Project.owner_id` → `User.telegram_id`
  *before* publishing, including the sites that used to publish `user_id=""`
  with a "StoryDTO has no user_id field" comment, and a recipient that cannot be
  resolved raises an admin alert instead of vanishing. PO keys its thread on the
  chat (`po-chat-{id}`), so a user's message and a pipeline event about their
  project share one conversation, and it refuses to answer a user-facing event
  that arrives without a recipient. The bot's proactive delivery moved to
  `telegram_bot/src/proactive.py`: bounded retries, distinguishable
  success/exhaustion logs, and an admin alert naming story, project and event
  when the attempts run out.

- Made that separation fail closed. A payload still carrying the removed
  `user_id` is now rejected by every addressable contract instead of validating
  with its recipient silently dropped; the consumers that see the rejection log
  it and alert admins with story, project and event
  (`shared/contracts/recipient.py`). `DeployMessage` requires either
  `telegram_chat_id` or an explicit `unaddressed_reason`, so admin-initiated
  application actions and temporary-access deploys state why they report to
  nobody rather than leaving an empty field; an owner-requested project teardown
  resolves the owner's chat like any other user-facing lifecycle message. The
  bot's proactive listener no longer auto-acks: it claims the pending entries of
  its previous incarnation on startup and bounds retries by the consumer group's
  delivery count, so a delivery interrupted by a restart is retried and
  eventually alerted about instead of sitting in the PEL forever. The API's
  unresolved-recipient alert now names the story as well as the project.

- Restricted live cleanup and write-ahead deployment recovery to API server
  rows authorized by the existing managed Time4VPS provisioning policy.
  Unrelated inventory rows are logged and skipped before SSH-key retrieval or
  SSH, while missing keys, invalid admitted connection data and an empty
  managed target set remain fail-closed errors.

- Made worker-manager the sole producer of Docker-global identities in generated
  Compose plans. Build services receive a manager-derived per-worker/service
  output tag, while source-declared volume `name` and `container_name` fail
  closed. Resolver-materialized volume names are removed from the immutable
  plan so the fixed Compose project name derives them again at execution.

- Replaced worker-manager's source-only Compose admission table with one
  host-capability policy for resolution and build execution. Generated builds
  now admit only static workspace-contained `context`/`dockerfile` and build
  `args`; cache import/export and every other pinned Compose v2.27.1
  `BuildConfig` property fail before resolution, are rechecked after resolution,
  and cannot be written into an immutable execution snapshot.

- Added a finite Compose v2.27.1 source-directive admission table to
  worker-manager. `label_file` and unsupported loaders are rejected before
  configuration resolution, supported static sources retain their contextual
  workspace checks, and the immutable execution snapshot rejects retained
  loader directives.

- Hardened worker-manager's broker-authenticated Compose boundary. Container
  creation now admits only scoped safe command arguments, so runtime mounts,
  capabilities, ports, names, environment and identity overrides cannot bypass
  policy. The runner applies bounded CPU and memory overrides for every selected
  service in the same fixed invocation used for resolution and execution.
  Resolved workspace-relative binds remain supported, while outside binds,
  sockets, namespace opt-outs, non-worker networks and host-exposing named
  volumes fail closed. Focused coverage includes actual service-template Compose
  resolution when the Docker CLI is available.

- Reworked Compose execution into a runner-owned plan compiler: creation resolves
  and validates source and effective configuration, writes a restrictive
  manager-owned snapshot, and executes only that snapshot. The recovery profile
  now permits scoped `down -v`, does not reread a hostile manifest, and removes
  the worker plan after teardown. Limits remain project-declared when valid, with
  feasible defaults only for services that omit them.

- Fixed effective Compose build validation to resolve relative Dockerfiles from
  their validated build context, preserving safe service-template builds while
  rejecting a Dockerfile path that escapes the worker workspace.

- Pinned worker Compose source validation and execution to the first selected
  file's project directory, so multi-file source paths cannot resolve against a
  different base before inspection. Build-network selection is now unsupported,
  and recovery commands reject worker-selected Compose files.

- Corrected Compose source-tree path contexts: selected files use the fixed
  project directory, while files loaded through `extends` use their own
  directory for nested source paths. Recovery now distinguishes global file
  selection from the supported `logs -f` follow flag.

- Restricted source-only Compose path fields to static literals, rejecting
  interpolation in `env_file` and `extends.file` before project environment
  values can select a host path during configuration resolution.

## 2026-08-09

- Added executable worker-broker acceptance regressions: resolved production
  Compose topology now protects the worker-only broker contour, and a
  deterministic broker flow covers internal-token denial, registration, input
  lease, session/status, typed output acknowledgement and Compose forwarding.

- **Worker broker launch contract**: coding workers now receive only
  `WORKER_BROKER_URL`, a per-worker broker token and `WORKER_ID`; wrapper
  configuration maps that exact identity and fails closed when a direct Redis,
  API, manager or encryption transport value is present. Removed the shipped
  Redis session adapter and wrapper Redis dependency. The broker internal
  credential is required and non-empty for manager and broker settings, is
  provisioned by deploy and every Compose harness, and is documented in the
  deployment and secrets guides. DinD/E2E harnesses launch a broker and route
  Compose through it. Worker input and output streams now have documented
  bounded retention, alongside broker session expiry and credential-scope tests.

- Added the authenticated worker-broker boundary for coding-worker control-plane traffic.

- Coding-agent subprocesses now inherit an explicit, documented allowlist
  instead of the worker-wrapper container environment. Normal launches and
  Claude auto-resume share it, preserving required CLI process basics,
  per-agent authentication/session settings, Claude's disabled updater and
  telemetry settings, `PYTHONNOUSERSITE`, and repository-scoped GitHub
  credentials while excluding wrapper transport URLs, encryption material,
  Docker/Compose configuration, host paths and arbitrary command variables.
  Transcript redaction continues to use the complete wrapper environment, so a
  secret excluded from the child environment remains scrubbed from artifacts.

- Worker-manager now resolves scaffolded workspace entries against the configured
  root and accepts only a single direct child. Absolute ids, traversal,
  multi-component ids and symlinks resolving outside that root fail before
  ownership preparation, volume construction, container launch or git setup.
  Workspace cleanup uses the same guard, so an unsafe entry cannot recursively
  remove a path outside the configured root.

- Coding-worker Docker launches now take their runtime kwargs from
  `WorkerContainerConfig`: Claude, Codex and Factory are capped at 4g and one CPU,
  with a 256-PID ceiling, all Linux capabilities dropped, and
  `no-new-privileges:true`. Empty network configuration resolves to
  `WORKER_NETWORK`; production rejects `DOCKER_NETWORK=host` before creation.
  The existing DinD host-network fixture declares `ENVIRONMENT=test` explicitly,
  preserving its isolated test-only compatibility path. Worker-manager now
  normalizes workspace and transcript bind-mount ownership from its trusted
  root context before launching the capability-restricted container, and aborts
  on a failed `chown`; the old in-container ownership repair was removed because
  `cap_drop: ["ALL"]` correctly prevents it.

## 2026-08-07

- **The Claude worker keeps its CLI config in the mounted host session directory**
  (`issue:3b54bc1fe7b141dfa0f6`): in `auth_mode=host_session` worker-manager mounted only the
  directory (`HOST_CLAUDE_DIR` → `/home/worker/.claude`), while the CLI's main config file is
  `~/.claude.json` — outside that mount. So every run wrote the config into the container's
  ephemeral layer and its backups into the mount, and the next container started from empty
  state: on the acceptance run of 2026-08-07 the engineering worker died with `Claude
  configuration file not found at: /home/worker/.claude.json / A backup file exists at:
  /home/worker/.claude/backups/…`, and the host directory held nothing but two 50-byte backups,
  one per run. `WorkerContainerConfig.to_env_vars` now exports `CLAUDE_CONFIG_DIR` pointing at
  the same path the host directory is bound to, which is where the CLI keeps `.claude.json`,
  `backups/` and the session together — one host-owned directory, no single-file bind mount
  (inode-bound, and the CLI rewrites the file) and nothing new in the image. The worker now also
  learns the mode it was created with (`WORKER_AUTH_MODE`) and, in `host_session`, refuses to
  start unless that directory is set, mounted from the host and writable — naming what is
  absent, instead of failing in the middle of an engineering round. The directory baked into
  `worker-base-claude` is not a mount, so a worker that lost its `HOST_CLAUDE_DIR` can no longer
  start quietly on empty state; `api_key` mode keeps no session and is unaffected.
  `.credentials.json` and the Codex branch (`host_codex_home`, `CODEX_HOME`) are untouched, and
  nothing new crosses the deploy boundary: `CLAUDE_CONFIG_DIR` and `WORKER_AUTH_MODE` are
  container-internal, `HOST_CLAUDE_DIR` remains the only deploy secret involved, so the list in
  `docs/DEPLOY.md` is unchanged.

- **Live-harness clients pass the global auth gate, and each kind is built in one place**
  (`issue:1bb703d5eea4337b143c`): since the gate landed in #233, `X-Telegram-ID` names an actor
  but authenticates nobody, so the mega's user client — which sent only that header — was answered
  `401 Unauthorized` on `POST /api/users/upsert` and the run ended in `ensure_test_user` after
  0.21s. `tests/live/pipeline_helpers.py` now owns the three client kinds as three factories whose
  names say which is which: `api_client_as_test_user` (internal key + `X-Telegram-ID`, the product
  path), `api_client_as_internal_service` (internal key, names no user — the endpoints gated by
  `require_internal_or_admin`), and `api_client_as_unscoped_observer` (internal key and never a
  user header, so `list_runs` does not narrow away the unowned deploy and QA runs — the defect
  hotfix #232 repaired, now asserted at construction, not only documented). Every call site in
  `tests/live/` takes a client from a factory; none composes auth headers of its own, and the
  helpers no longer re-send the key per call. A missing `INTERNAL_API_KEY` is refused after
  collection and before the first test with a sentence naming the variable, instead of a
  `KeyError` from the first client or a 401 halfway through a 30-minute run. That refusal is
  scoped to runs that call the API: `tests/live/` also holds regressions that call it never — the
  offline group drives these same helpers against fakes, and the Redis cleanup regression talks
  only to a container — and CI's fast-checks runs both with no key in the environment, so
  demanding it for the whole session aborted that job with `INTERNALERROR` before a single test
  was collected. Needing the API is the default and the exception is declared —
  `pytestmark = pytest.mark.needs_no_api_credential` — so a module that forgets the marker fails
  loudly rather than quietly losing the guard; a test runs both of CI's keyless selections for
  real with the variable unset, and another reads the Makefile's offline ignore list so that set
  and the markers cannot drift apart. Harness-only change; no product code, no API rule, no
  contract touched.

- **`docs/DEPLOY.md` lists the secrets the deploy actually reads** (`issue:3c2e590d60545c99de29`):
  the secret tables and `.github/workflows/*.yml` had drifted apart in both directions after the
  #227–231 hotfixes, so an operator configuring a deploy strictly from the document failed the
  workflow's required-secret preflight. Now reconciled and checkable in one command:
  `diff <(grep -rhoE 'secrets\.[A-Z0-9_]+' .github/workflows/*.yml | sed 's/secrets\.//' | sort -u)
  <(grep -oE '^\| `[A-Z0-9_]+`' docs/DEPLOY.md | tr -d '|` ' | sort -u)` prints nothing.
  Added: `ARCHITECT_LLM_MODEL|BASE_URL|API_KEY` to LLM Providers (with the all-or-nothing rule that
  keeps a half-configured agent out of the pipeline), `TELETHON_API_ID|API_HASH|SESSION` to the
  Telegram table rather than only in QA prose, and a new LK section for `LK_DOMAIN` and
  `LK_JWT_SECRET`. `GITHUB_ORG` became `GH_ORG`, with the point spelled out that the rename stopped
  at the secret — services still read the env var `GITHUB_ORG`. `GITHUB_WEBHOOK_SECRET` was dropped:
  the webhook it signed for was removed in b6b7310f and nothing in the tree reads it. The GitHub App
  key now documents its whole path — `GH_APP_PRIVATE_KEY` → `/opt/secrets/github_app.pem` on the
  host → read-only bind mount → `GITHUB_APP_PRIVATE_KEY_PATH=/app/keys/github_app.pem` in the
  container — since the host and container paths differ and neither is a GitHub secret.
  No workflow, compose or service behaviour changed.

- **A throttled poll is no longer a verdict on the reinstall** (`issue:a29c4b89a061cada8ad1`):
  Time4VPS answers a too-fast second action on one server with `401` and
  `{"error":[["wait_x_between_action",24],"unauthorized"]}` — the same status it uses for a real
  loss of authorization. `Time4VPSAPIError.rate_limit_wait_seconds` classifies the two by that key
  in the body, never by the status code, and returns the interval the provider asked for; a `401`
  without the key stays fatal. `wait_for_task` treats exactly that one answer as transient: it
  waits the stated interval, clipped to what is left of the caller's `timeout`, and polls on.
  Every other provider error still ends the wait. This matters because the completed task's
  `results` is the only carrier of the new root password — on 2026-08-06 a reinstall of vps-275301
  really succeeded 11 seconds after such a `401`, and the orchestrator recorded "Reinstall failed"
  and lost the password. `wait_for_password_reset` is now `wait_for_task` plus `extract_password`
  instead of a second copy of the polling loop, so the explicit-reset fallback is covered by the
  same rule. No general retry layer was added: only task polling, only this one stated refusal.

  Relatedly, the SSH-key persistence failure branch of `handle_provisioning_success` now owns its
  outcome the way every failure branch in `node.py` does — `update_server_status(..., "error")`
  plus a `PROVISIONING_FAILED` incident. Before, it returned `failed` with a reason and nothing
  else, so the scheduler's `UNREACHABLE` could later be undone by `server_sync` into `ACTIVE`,
  leaving a server with no key in the DB and no incident to make it retryable. The success side is
  unchanged: the reset endpoint remains the single owner of the terminal READY status.

- **The live harness owns a deploy before it exists** (`issue:47affbe42eb8ad5c16ef`): a live run
  records `server_deployment <slug>` in its ownership manifest before any deploy run can start,
  instead of only after the application reported `RUNNING` and its port
  allocation had been read. `wait_deploy` now *enriches* that same record with `server_handle`
  and `server_ip` — `OwnershipManifest.own` merges metadata into an existing `(kind, identifier)`
  record rather than appending a second one — so a failure anywhere between `docker compose up`
  on the target and that enrichment still leaves teardown holding the stack name. Teardown of a
  single run reads the manifest and nothing else; a record with no resolved target is cleared on
  every server the API lists (`shared.live_harness_cleanup server-cleanup` takes `--server-handle`
  as optional and reads `ssh_user`/`public_ip` from the same server DTO, so the `127.0.0.1`
  fallback is gone).

  `make test-live-clean` also stopped reporting a clean host while a foreign stack was running on
  a deploy target: it inventories the targets themselves (`docker ps` plus `/opt/services/*` by
  the deployed-slug prefixes derived from `PROJECT_PREFIXES` via `shared.project_slug`), sweeps
  the stacks it finds even when no database row names them, and counts them in the final residue
  verdict with the findings listed. Prefix sweeping stays confined to that global cleanup — one
  run's teardown never uses it.

  Which runs take the write-ahead record is derived, not declared: a run owns the stack when it
  creates its story (`create_story_and_task`), because the story PR's merge is what makes
  `pr_poller` create a deploy run at all. There is no `deploys=` flag to set at a call site and
  no flag to forget — a new live test that drives engineering owns its stack without knowing the
  rule exists, and a scaffold-only run, which creates no story and so can reach no deploy, owns no
  stack and touches no server on teardown, so one unreachable target cannot fail a test that
  deployed nothing. `wait_deploy` takes the same record again on entry (owning twice merges), so a
  run that ever reaches a deploy by some other route is owned rather than orphaned. The target
  inventory is fail-closed — an unreachable `docker` on a target fails
  the scan instead of reporting an empty, falsely clean host. `scripts/clean_live_tests.py` also
  lost its literal `\n`/`\|` escapes: psql answers are split on real newlines (two live-test
  projects used to collapse into an empty project list, emptying the DB half of both the sweep and
  the residue verdict, and every active repository workspace looked orphaned), and the local
  container filter is a real regexp alternation. An unprovable ownership manifest now fails the
  cleanup at the end rather than aborting it at its first step, so every other sweep still runs.
  `tests/live/README.md` describes the teardown scope as it now is.

- **Provisioning success commits its result in one order** (`issue:23593a6a2850ae9c7964`):
  `handle_provisioning_success` now persists the server's private SSH key **first**, then closes
  the episode. A missing `ssh_manager`, an empty private key, or a failing `save_server_ssh_key`
  is a failed provisioning (`status: "failed"` with a `reason`, logged as
  `provisioning_ssh_key_persist_failed`) instead of a silently skipped `if`, and the superseded
  branch (`reset == False`) can no longer return before the key is stored — the key of a server
  that was really provisioned always lands in the DB. The keys live in the infra-service
  container's ephemeral filesystem, so a skipped save meant permanent loss of access to that
  server.

  The terminal status has a single owner: the `provisioning-attempts/reset` endpoint, which writes
  `attempts = 0`, clears the episode and sets `READY` in one conditional UPDATE. The handler's own
  unconditional `update_server_status(..., "ready")` and the scheduler result listener's
  `ACTIVE` write on success are both gone; neither can overwrite a status the newer episode owns.

  The infra integration test that asserted the old side-write
  (`test_provisioner_success_flow_updates_server_to_active`) now encodes the new contract as
  `test_provisioner_success_result_does_not_overwrite_terminal_status`: it waits until the
  scheduler's consumer group has actually consumed and ACKed the success entry, then asserts the
  server's status is unchanged.

## 2026-08-06

- **The API answers nobody anonymously** (`issue:a625fbca694614214ea5`): one dependency on the
  FastAPI application, `require_authenticated_caller`, now stands in front of every route. It
  admits a valid `X-Internal-Key` or an LK bearer token and nothing else; `X-Telegram-ID` on its
  own is refused with 401, so the header can no longer be used to act as a user. The routes that
  stay anonymous are listed with a reason each in `ANONYMOUS_ROUTES`: `GET /`, `GET /health` and
  `POST /api/lk/auth/token`. `routers.debug` moved from beside `/health` to `/api/debug/*` and is
  closed like everything else. `POST /api/users` and `/api/users/upsert` refuse `is_admin` from a
  non-internal caller with 403, while the bot's registration of `ADMIN_TELEGRAM_IDS` keeps working:
  a worker container that can reach the API's port can no longer write itself an administrator and
  then act as it.

  `services/api/tests/unit/test_global_auth_gate.py` walks `app.routes` rather than a hand-written
  list, so a router included without going through the gate fails the suite instead of shipping.

  **Callers that were reaching the API without a credential and are fixed here**: the admin
  frontend's nginx proxy (now stamps `X-Internal-Key` into what it forwards, so the browser never
  holds it and basic auth remains what decides who may use that origin), `make seed`,
  `scripts/seed_agent_configs.py` and `scripts/seed_system_configs.py` (now on
  `InternalAPISyncClient`), `scripts/test_e2e_flow.py` (now on `InternalAPIClient`),
  `infra/scripts/{ssh-to-server,dump-server-keys,restore-server-keys}.sh`, the `tests/live` harness
  fixture, `tests/e2e/test_engineering_flow.py`, and the curl recipes in the pipeline skills.

## 2026-08-04

- **Fail-closed Time4VPS provisioning guard** (`issue:a6e238c69a60b84a1745`, hotfix):
  provider discovery no longer treats every VPS in the account as an orchestrator target. Only IDs
  explicitly listed in `TIME4VPS_MANAGED_SERVER_IDS` become managed and enter `pending_setup`;
  absent configuration denies all, and every other server is recorded as unmanaged/reserved.
  Existing records are reconciled by immutable provider ID rather than IP, management changes alert
  admins, and promotion of an existing row never schedules work. Every scheduler publication path,
  infra-service and the destructive operation boundary enforce the same policy before state writes.
  A failed SSH probe no longer selects reinstall: only an explicit force-rebuild request can do so.
  `GHOST_SERVERS` is removed, the production workflow preserves the required allowlist secret, and
  provider ID/IP binding (including a required non-empty provider IP) is revalidated immediately
  before provisioning. Demotion preserves operational status while neutralizing queued work, the
  legacy `force_reinstall` queue flag was removed end to end, and production deploys now preserve
  the provider credentials and orchestrator public IP required by the guarded path. Unauthorized
  stale scheduled rows are moved to `reserved` with an admin alert instead of retrying forever.
  Force-rebuild publications use the full stuck timeout, preventing duplicate queued provisioning
  while the single infra-service worker is busy.

- The last eight duplicated request schemas have one definition each, and no class name is now
  defined in both `services/api/src/schemas/*` and `shared/contracts/dto/*`. `ApplicationCreate`,
  `ApplicationUpdate`, `IncidentCreate`, `ServerCreate`, `StoryCreate`, `StoryUpdate` and
  `TemporaryAccessGrantCreate` live in the contract and the API re-exports them; field sets follow
  the model columns. `RunCreate` was closed by deletion instead: the contract copy carried
  `project_id`/`type`/`spec`, `Run` has no `spec` column and nothing imported that class, so it is
  gone and the live server definition (every field a `runs` column) moved into the contract.

  **Wire behaviour**: three request fields become stricter, none looser. `ApplicationCreate.status`
  and `ApplicationUpdate.status` are `ApplicationStatus` rather than free `str`, `IncidentCreate`
  takes `affected_services: list[str]` rather than `list`, and `TemporaryAccessGrantCreate.project_id`
  is a UUID rather than any non-empty string — each matching what its column stores. `ServerCreate`
  keeps the contract's `status: ServerStatus` and its `DISCOVERED` default, which is also the
  column default; the server copy's `str` field defaulting to `active` is gone. `ServerCreate` also
  loses `provider_id`, which `Server` has no column for (it is read from `labels`) and no handler
  ever read; `scheduler`'s discovery already sent it inside `labels` and no longer passes it
  separately.

- Eleven more request schemas have one definition each. `AnalyticsDailyCreate`,
  `AnalyticsHourlyCreate`, `AnalyticsKnownUserUpsert`, `AnalyticsKnownUsersBatchUpsert`,
  `IncidentUpdate`, `RepositoryCreate`, `RepositoryUpdate`, `TaskCreate`, `TaskEventCreate`,
  `TaskUpdate` and `TemporaryAccessGrantUpdate` were declared identically in
  `shared/contracts/dto/*` and in `services/api/src/schemas/*`; the server copies are deleted and
  the schema modules re-export the contract classes, so the API validates against the object its
  clients send. Field sets and types are unchanged. Two non-field differences were carried over to
  the contract rather than dropped: `TaskUpdate` keeps `extra="forbid"`, which only the server copy
  had, and `TemporaryAccessGrantUpdate`'s refusal of `REVOKED` keeps the server's longer message
  naming the observation endpoint. `tests/unit/test_request_schemas_are_not_duplicated.py` now
  asserts object identity for all thirteen merged names and lists the eight still duplicated.

- The dead layer is out of the tree. `LLMNode` (`services/langgraph/src/nodes/base.py`) and the
  three modules only it used are deleted: `nodes/tool_executor.py` (`ToolExecutor`), `llm/`
  (`LLMFactory`) and `config/agent_config.py` (`get_agent_config`, `invalidate_cache`, the TTL
  cache), along with the `llm/` and `config/` re-exports. The subgraphs take only `FunctionalNode`,
  `RetryPolicy` and `log_node_execution` from `nodes.base`, and those stay; the PO and architect
  agents build their own `ChatOpenAI`, so `langchain-openai` stays a dependency.
  `services/langgraph/src/redis_publisher.py` had no reference anywhere and is deleted too.
  `services/langgraph/tests/unit/test_dead_layer_removed.py` fails if any of them comes back.

  **External HTTP contract**: two routers are no longer mounted, so five paths are gone from the
  API. `services/api/src/routers/resources.py` is deleted, taking `GET /api/resources`,
  `POST /api/resources` and `GET /api/resources/{handle}`; the `Resource` model and its table are
  untouched. The two OpenRouter catalogue endpoints, `GET /api/available-models` and
  `GET /api/available-models/{model_id}`, are gone with the router object in
  `routers/available_models.py`; the module itself stays, because `routers/agent_configs.py` calls
  its `validate_model_identifier` when a config's `model_identifier` is written. No caller was found
  for any of the five paths in any service or in either web client.

  Two alias blocks are gone as well: the thirteen `_`-prefixed re-exports in `routers/rag.py` and
  the seven in `routers/tasks.py`. Neither block had a consumer; the public names they aliased are
  unchanged.

- `ProjectCreate` and `ProjectUpdate` have one definition each. `shared/contracts/dto/project.py`
  and `services/api/src/schemas/project.py` each declared a class of that name, and the two field
  sets had drifted apart in both directions: the contract carried `description` and `modules`, for
  which the model has no column, and lacked `config`, which it has; the server's `ProjectUpdate` had
  no `project_spec` and forbade extras, so `github_sync` PATCHing a spec read out of
  `.project-spec.yaml` got a 422 and the spec never reached the database. The API now validates
  against the contract classes it imports, `patch_project` and `update_project` carry `project_spec`
  onto the row like any other field, and `ProjectRead` returns it, so the architect's
  `get_project_spec` reads back what the sync wrote. `description` and `modules` are gone from both
  request schemas: the PO agent, the scaffolder and the developer node all read them out of
  `config`, where they are actually stored, and a top-level one was being dropped in silence.
  `status` is typed `ProjectStatus` on both, so a status the enum does not define is now a 422 —
  three service tests were creating projects with `"created"`, a `StoryStatus` value that projects
  never had, and one integration test with `"scaffold_failed"`, which migration `b3c4d5e6f7a8` took
  out of the enum. `tests/unit/test_request_schemas_are_not_duplicated.py` fails on any new class name
  defined in both trees; the 19 names still duplicated are listed there, and the list is checked for
  stale entries so it can only shrink. Tests:
  `services/api/tests/service/test_project_spec_sync.py`.

- Every internal API call carries `X-Internal-Key` and `X-Correlation-ID`, and none of them is
  written by hand any more. The transport used to send the correlation header only when something
  had already bound one, so the bot (which binds nothing) and the scheduler's background loops went
  out unlabelled; `ensure_correlation_id()` now creates the identifier and binds it to the current
  context, so the rest of that flow reuses it instead of taking a fresh one per call. Two callers in
  `shared/` skipped the transport entirely and sent neither header: `shared/config_store.py` read
  `/api/system-configs/...` with two bare `httpx.get` calls at scheduler and PO startup, and
  `shared/notifications.py` read `/api/users` with bare `aiohttp` on every admin alert. Both go
  through the shared transport now — the store through `InternalAPISyncClient`, the synchronous form
  of it, since it is read from synchronous startup code. `notify_admins` raises
  `httpx.RequestError` instead of `aiohttp.ClientError` when that read fails; its aiohttp use for
  Telegram itself is unchanged. The static guard was the reason both survived card 1139: it read
  only `services/`, so `shared/` was never checked. It reads both trees now and matches on a rule,
  not a file list — a module that builds its own HTTP client aimed at the internal API, or any
  request whose URL comes from that base URL, including through a local variable and including
  libraries other than httpx. Which client class a module built is resolved through its imports, so
  `from httpx import AsyncClient`, `import httpx as h` and the same forms of `aiohttp` are one
  finding rather than one matched spelling. Both mandatory headers are set canonically and once:
  every case variant of the two names is taken out of the caller's headers first, since
  `x-internal-key: forged` used to be copied through and left ahead of the transport's own field,
  and the API reads the first field of a name. A caller-named correlation ID is read in any spelling
  and becomes the identifier of the flow. `shared/live_harness_cleanup.py` stays out (live-stand
  path, out of the card's scope; the exemption is recorded in the sprint entry and is to be filed as
  an issue at closing) and is the guard's only exemption besides the transport module itself.
  `test_no_correlation_id_bound_means_no_header` asserted the old rule — no binding, no header — and
  is replaced by `test_an_unbound_context_gets_an_id_from_the_transport`. Tests:
  `shared/tests/unit/test_internal_api_transport.py`, `shared/tests/unit/test_config_store.py`,
  `shared/tests/test_notifications.py`.

- A valid `X-Internal-Key` authenticates a service; it no longer makes that service anyone's
  deputy. The guards used to return on the key alone, so once every caller sent it — the PO agent
  and the bot included, and they name the end user in `X-Telegram-ID` — a Telegram user could have
  asked the agent for a stranger's project or run and got it: `get_project`, the secret endpoints,
  `teardown`, `get_run_status`. A request that names a user is now judged as that user, key or no
  key; a service call that names none is unchanged, and an admin still reaches everything.
  `resolve_actor` in `services/api/src/dependencies.py` is the single place that decides who is
  acting, and the only place allowed to read the internal flag: `_check_project_access`,
  `_check_run_access`, `require_internal_or_admin` and the two list endpoints ask it instead of
  deciding for themselves. Writing the rule out by hand per guard is what let it be applied in
  `projects.py` and missed in `runs.py`, so a test now fails when any other function reads the flag
  — a guard nobody has written yet included. `GET /api/servers` requires an admin on the server
  again, as it did before the transport work. Tests:
  `services/api/tests/unit/test_internal_flag_has_one_reader.py`,
  `services/api/tests/service/test_internal_key_is_not_impersonation.py`.

- `docker/test/service/telegram_bot.yml` gives the bot `INTERNAL_API_KEY`, which the shared
  transport reads at import: without it the container died on a `KeyError` while the suite still
  reported a green smoke test from its runner. The bot's import is a healthcheck the runner waits
  on now, so a bot that cannot start is a red suite instead of a green one with a dead service.

- The transport to the internal API is written once, in `shared/clients/internal_api.py`. Five
  services carried a near-identical `_get_client` / `_api_path` / `_request` — 1384 lines of client
  code between them — and the copies had drifted: `services/telegram_bot` sent no `X-Internal-Key`
  at all, and the PO tools bypassed their client entirely, holding a module-level `httpx.AsyncClient`
  handed out by `init_po_clients()`. `SchedulerAPIClient`, `LanggraphAPIClient`,
  `InfrastructureAPIClient`, `ScaffolderAPIClient` and `TelegramAPIClient` keep their names and their
  application methods and now subclass `InternalAPIClient`; the only difference that survives, the
  bot's 10-second timeout, is a constructor argument. `X-Internal-Key` and, when one is bound,
  `X-Correlation-ID` go on every request from inside that module, so no caller can forget them:
  `_get_api()` hands the PO tools the same client the rest of `langgraph` uses, and
  `worker-manager`'s workspace-GC notification, the last raw `httpx` call to the internal API, goes
  through it too. On the wire nothing else changed — same `/api` prefix, same refusal of an `api/`
  path or an `API_BASE_URL` ending in `/api`, same `raise_for_status()`. Callers that read a status
  code themselves (a 422 verdict, a 404 that means "not yet") use `request_raw` and its
  `get_raw` / `post_raw` / `patch_raw` shorthands. Tests:
  `shared/tests/unit/test_internal_api_transport.py` fails if a service grows its own `_request` or
  its own `httpx.AsyncClient` aimed at the internal API, and
  `services/telegram_bot/tests/unit/test_api_client_headers.py` fails if the bot's calls lose either
  header.

- The freshness check now answers for the whole tree, not for the part it happened to be able to
  compare. Every Dockerfile that bakes `shared` has to reach a declared image name through a build
  route — a compose service with an explicit `image:`, or a Makefile recipe that builds it under an
  explicit `-t` tag — and one that no route reaches fails `make check-shared-freshness` by name. A
  compose `image:` counts as declared only when it is a literal: `image: ${SOMETHING}` is resolved
  outside the tree, so it names nothing the check can inspect and it fails as an unreadable route,
  the same rule `is_pinned_image()` in `scripts/check-ci-gate.py` applies to a pulled reference.
  Before this, a Dockerfile that copied `shared` and stamped its label correctly but hung off no
  compose service and no recipe was compared with nothing and the check said nothing; that was true of
  nine of the seventeen files that bake `shared`, `services/scaffolder/Dockerfile` among them. Eight
  of the nine already had a route and were merely outside the label comparison because their service
  mounts `./shared:/app/shared` and runs the tree; the ninth,
  `packages/worker-wrapper/Dockerfile.test`, is deleted — nothing has built it since `2621eb42`
  dropped its make target in March, and the suite it would have run
  (`tests/integration/worker_wrapper`) is red and already tracked in `scripts/check-ci-gate.py`. The
  Makefile side is read the same way as compose from now on: every recipe is parsed, a `docker build`
  of a Dockerfile that bakes `shared` owes `--build-arg SOURCE_HASH` and a tag, and a build that does
  not say which Dockerfile it builds fails the check instead of being skipped. There is no list of
  exceptions, deliberately. A machine without docker, or without a reachable daemon, now reads as
  "nothing built" for every image rather than crashing, so the static half of the check gives the same
  answer with docker and without it. Tests: `scripts/tests/test_shared_freshness.py`; docs:
  `docs/REBUILD.md`.

- A built stand can no longer be quietly behind the tree on `shared`. `make check-shared-freshness`
  (`scripts/shared_freshness.py`) compares the source hash baked into every reused image with the hash
  of the tree and exits non-zero on a difference, naming the image. It reuses what the worker circuit
  already had — `--build-arg SOURCE_HASH` and the `org.codegen.worker_source_hash` label — instead of
  adding a second mechanism, and the hash itself is now computed in that script and read from there by
  the Makefile and by the two fixtures that build worker base images, so there is one counter rather
  than several that can drift. Coverage is derived from the tree, not listed: every Dockerfile in the
  repository is parsed in every `COPY` form docker accepts, and every compose file is parsed,
  `docker/test/**` included. A service built from a Dockerfile that bakes `shared` has to pass
  `SOURCE_HASH` in `build.args` and to declare an explicit `image:` name, so the images the test
  compose files leave behind are checked like any other; the services that mount
  `./shared:/app/shared` run the tree and are not compared. Nothing that cannot be read is allowed to
  pass: an unreadable `COPY`, an unparsable compose file, a Dockerfile without `ARG SOURCE_HASH` and
  the label, or a compared image whose label is missing, empty or not a hash all fail the check by
  name. An image that is not built is not behind anything and does not fail the check — that is what
  keeps it green in CI, where it now runs in `fast-checks`. It builds nothing, starts nothing and uses
  no network. Tests: `scripts/tests/test_shared_freshness.py`; docs: `docs/REBUILD.md`.

## 2026-08-03

- `shared` has one declared form left, and it is the tree. The three editable entries in the root
  `[tool.uv.sources]` (`codegen-orchestrator-shared-contracts`, `-redis`, `-log-config`) and the
  three `pyproject.toml` files behind them are gone: nothing depended on those names, they were
  absent from `uv.lock`, and they installed nothing while reading like a package boundary.
  `docs/REBUILD.md` names the delivery channels including the import from the tree over `PYTHONPATH`.
  `tests/unit/test_uv_sources_are_used.py` fails if a source entry that nothing depends on comes
  back. `shared/pyproject.toml` is untouched and stays the single declaration of `shared`'s
  third-party dependencies; no Dockerfile and no compose service changed, and `uv.lock` did not move.

- The last floating base image tags are gone, and the gate keeps them gone. Both
  `COPY --from=ghcr.io/astral-sh/uv:latest` lines name `0.12.1`, the version the built worker
  image and the registry tag both report today. The three derived worker images
  (`worker-base-claude`, `-codex`, `-factory`) declare `ARG BASE_IMAGE` without a default, so
  the builder has to name the common image it just produced: `make rebuild-worker-images` tags
  common with `WORKER_SOURCE_HASH` as well as `:latest` and passes the hash tag, and the backend
  integration conftest passes its own content-hash tag instead of hanging a `:latest` alias on
  it. A build that forgets the argument fails on a blank base name. `make ci-contract` now walks
  every Dockerfile and compose file in the tree and fails, with file and line, on an image with
  `:latest` or no tag at all; a reference left floating on purpose needs an entry in
  `UNPINNED_IMAGE_REFS` or `UNPINNED_IMAGE_DIRS` with a reason. In a compose file it reads a
  service's `image` directly or through YAML merge keys, and a service with only a `build` is no
  violation, since the Dockerfile it builds is walked separately. What it cannot read it does not
  wave through: `extends`, an unresolvable anchor, an `image` that is not a single value, or a
  file that does not parse fail the gate by name, so a shape the gate does not follow can never
  pass as if it had been checked.

- The CI contract gate derives the list of test files from the tree instead of trusting a
  constant in its own head. `scripts/check-ci-gate.py` walks the repository for files pytest
  would collect and fails when one is run by no CI target, so a new test can no longer be
  invisible. Claims are read off the targets themselves: the `ALL_SUITES` table in
  `scripts/test-unit-local.sh`, the pytest commands in the `docker/test/{service,integration}`
  compose files behind the two CI matrices, and the pytest commands of an explicit
  `test-integration-<suite>` Makefile target. A claim covers what its target runs and nothing
  more: a directory argument covers the directory recursively, a file argument covers that one
  file. The service and integration matrices are likewise checked against those compose
  directories rather than against hardcoded sets. A test that is deliberately not run has to be
  listed in `UNCLAIMED_TEST_FILES`, or its directory in `UNCLAIMED_TEST_DIRS`, with a reason.

- `services/scaffolder` joins the uv workspace, and its unit tests, along with `scripts/tests`,
  `packages/worker-wrapper/tests/{component,integration}` and
  `services/telegram_bot/tests_legacy/unit`, now run on every PR through `make test-unit`. None of
  those five directories was executed anywhere before.

## 2026-08-03

- The tree now fixes what actually gets installed and run. `uv.lock` is committed instead of
  ignored, and both CI dependency steps install from it with `uv sync --locked`, which both
  refuses to re-resolve and fails the run when the lock has drifted from the `pyproject.toml`
  files. The integration, template, and E2E test requirement files pin
  exact versions, and every base image in the repository's Dockerfiles and Compose files carries
  an explicit tag instead of a moving one (`python:3.12.13-slim`, `redis:7.4.10-alpine`,
  `pgvector/pgvector:0.8.6-pg16`, and the rest). A red or green CI run now describes the tree, not
  the state of an upstream index.

- The production deploy brings the server to the commit the workflow run was dispatched on
  (`git fetch` of that SHA plus `git reset --hard`) instead of `git pull origin main`, which could
  deploy a newer branch tip than the one that was validated. A unit test reads `deploy.yml` and
  fails if the revision step goes back to `git pull` or to `origin/main`.

## 2026-08-02

- The production deploy now fails before touching the server when critical database, internal API,
  admin, Grafana, or Loki credentials are absent. It writes those credentials into the generated
  `.env` with mode `0600`, validates the merged Compose model before building, and probes API
  health from inside the API container because production intentionally has no host port 8000.
  `.env.example` now also declares the required `INTERNAL_API_KEY` instead of leaving new installs
  to discover that startup contract from an API validation error. The deploy runbook lists the
  corresponding internal API, Grafana database, Grafana admin, and Loki push secrets required in
  the GitHub production environment.

- Production Compose no longer publishes PostgreSQL, the admin frontend, or the user dashboard
  directly on host ports 5432, 3001, and 3003. Public user-dashboard traffic continues through
  Caddy at `/lk`; operator access to the admin frontend is limited to the internal network and an
  SSH tunnel until it has its own authenticated TLS route. This closes the direct dashboard API
  proxy and prevents Basic Auth from being sent over cleartext HTTP.

## 2026-07-28

- `PATCH /api/tasks/{id}` now rejects unknown fields instead of silently dropping them. In
  particular, sending `status` to the non-status update endpoint returns 422 and leaves the task
  unchanged, making callers use the explicit task transition endpoints.

- Offline live-harness contract tests now treat the checkout's `.live-manifests/` recovery
  directory as read-only. The fail-closed run-cancellation test writes its synthetic ownership
  manifest under `tmp_path`, and an autouse snapshot guard fails the exact test that creates,
  changes, or removes a real recovery manifest.

- Temporary test access to a deployed bot is now a durable state machine instead of two steps at
  either end of a successful QA run. The whole lifecycle is driven by a `temporary_access_grants`
  row: the deploy→QA handoff records what was given, to which project, on which commit, for which
  QA run, plus the deploy run that applies the value and the QA message being held. A scheduler
  sweep (`supervise_temporary_access`, which runs before stories are routed on their QA runs)
  moves it from there. QA starts only after the grant deploy has confirmed success, so a lagging
  grant deploy can no longer apply the value after a revoke already cleared it; a grant deploy
  that fails, is superseded, or never confirms clears the slot anyway and fails the QA run with
  `qa_access_grant_failed`. Revocation follows the same rule as before — any terminal state of the
  QA run, a run that disappeared, or a grant that outlived
  `supervisor.temporary_access_ttl_minutes` (a separate `temporary_access_grant_expired` event,
  an admin alert, and `qa_access_expired` on the run it outlived, not a quiet cleanup). Every
  deploy is of the granted commit through the pinned-commit deploy, so the bot that loses the
  access is the bot that was given it. Repeating a revoke that already landed is a redundant
  deploy, not an error. A revoke that keeps failing is retried quietly until
  `supervisor.temporary_access_max_revoke_attempts`, then reported to admins and turned into that
  QA run's failure (`qa_cleanup_failed`); the grant stays live and is still retried. Until the
  access is settled the story does not leave TESTING, so an unrevoked grant can no longer end as
  a completed story, and an escalated one reaches human review with the bot stopped. The bot's
  own admission rule now lives in `shared/contracts/bot_access.bot_admits`, so tests check that
  the environment a revoke ships refuses the QA identity rather than that a variable is absent.

  Three things the same lifecycle needed to hold under process death:
  giving up on a revoke is one write. `POST /api/temporary-access-grants/{id}/escalate` stamps the
  grant and records the `qa_cleanup_failed` outcome on its QA run in one transaction, so there is
  no state where one landed and the other did not, and a story is only let past a live grant when
  both are there. That call is also the one writer allowed the last word on a QA run's outcome: a
  run that borrowed a test identity has not finished while the identity is still admitted, so a
  worker's `passed` is provisional until the access is settled. Without it, a run the QA worker
  had already passed could never be told the access was stuck, and its story would sit in TESTING
  for good. Everything else still meets the ordinary refusal on `PATCH /api/runs/{id}`, so a
  worker reporting after an escalation is rejected and both orders end in the same place. A revoke
  deploy carries
  `DeployMessage.fence_active_deploys`: it skips the redundant-deploy shortcut and, through the
  new `GitHubAppClient.fence_workflow`, cancels every unfinished `deploy.yml` run and proves it
  terminal, so the abandoned grant deploy it replaces cannot land afterwards; a stop that cannot
  be proven refuses the deploy rather than recording the access as removed. The QA runner's
  Telegram `/start` probe moved to `shared/telegram_access_probe.py`, and
  `tests/live/test_bot_access_revocation.py` drives a real grant on a deployed bot, kills the QA
  run mid-flight, and requires that same probe to observe the bot refusing the QA account after
  the sweep revokes.

  The fence on GitHub Actions only reaches a deploy that already started. A grant deploy whose
  message is still queued is stopped by its own run instead: abandoning a grant cancels its deploy
  run before the clear is published, and the deploy consumer reads its run before it takes the
  project lock and refuses one that was cancelled. A grant deploy picked up late can no longer
  write the test identity back after the revoke landed.

  Four holes the same fence still had, closed by making each of them a state somebody can read:

  `GitHubAppClient.list_unfinished_workflow_runs` asked for one page of 50 and presented it as
  every unfinished run. A grant deploy queued behind a busy repository sits below every run
  started after it, so the revoke could clear the value and record the grant revoked while that
  run was still able to deploy the identity again. It now queries one unfinished status at a time
  and pages each to exhaustion, and a listing that runs past its page bound raises
  `WorkflowRunListingIncompleteError` rather than answering with a subset.

  Cancelling a run was not a fence for a deploy worker that had decided to dispatch but had not
  reached GitHub yet: no Actions run existed for the fence to find. The crossing is now recorded
  on the run under a row lock. `POST /api/runs/{id}/dispatch-claim` is taken immediately before
  every `workflow_dispatch` and every rerun, and refuses a cancelled run;
  `POST /api/runs/{id}/dispatch-withdraw` takes the same boundary from the other side and reports
  whether the deploy got out first. A refused claim ends the deploy as cancelled without
  dispatching.

  Two ways past that boundary remained. A worker read its run, found it live, and then wrote
  `running` blindly; a withdrawal landing in between was overwritten, and the resurrected run
  passed the dispatch claim afterwards. Taking a run to `running` is now the locked transition
  `POST /api/runs/{id}/start`, which refuses a run that is already terminal, and `PATCH
  /api/runs/{id}` refuses any move from a terminal status back to a live one, so no writer can
  undo a cancellation. And a withdrawal that arrived after the claim used to hold the revoke back
  until the claiming worker recorded its own outcome, which it writes on every path it can leave a
  deploy by. That is still the ordinary proof — but a worker that dies after claiming never writes
  it, and waiting on it left the grant unreconciled for good with an alert as the only trace.
  Elapsed time cannot replace it either: a paused worker still reaches `workflow_dispatch`
  afterwards. So holding the boundary is a lease. A claim carries a deadline (`DISPATCH_LEASE`,
  renewed each time the worker asks), the worker re-reads the clock immediately before dispatching
  and refuses once it has passed, and `POST /api/runs/{id}/dispatch-supersede` takes an expired
  claim back under the same row lock the claim was granted under — cancelling the run so it can
  never be re-claimed and stamping the crossing as superseded, which a restarted sweep reads
  without asking again. From there anything the dead worker did put on Actions is listable and the
  revoke's fence reaches it. A claim still inside its lease is waited for, and one that somehow
  stays live past `supervisor.temporary_access_revoke_stale_minutes` is reported as
  `temporary_access_grant_deploy_dispatch_stuck` with an admin alert. The QA run that borrowed the
  access is failed with its reason first, whatever the grant deploy turns out to have done.
  `supervisor.temporary_access_dispatch_settle_seconds` is gone.

  A deploy cancelled on GitHub Actions left its run at `running` with no result, which every
  supervisor skips for good — and a revoke's fence cancels ordinary story deploys as a matter of
  course, so this was a normal path. Cancellation is now terminal and typed
  (`DeployOutcome.CANCELLED`), and `supervise_deploying_stories` redeploys the story's commit
  under the same retry bound that stops a failing deploy from looping. The same is recorded when a
  deploy loses the project lock.

  The deploy→QA handoff had a crash window with nothing durable in it: story TESTING and a queued
  QA run existed before the grant did, so a process death in between left a story no supervisor
  was watching. The QA run is now created *before* the story leaves DEPLOYING and carries the
  whole `QAHandoffPlan` — the QA message and the access to be borrowed — in its `run_metadata`.
  Its id derives from the deploy run and the grant id from the QA run, so a repeat lands on the
  same records. `supervise_testing_stories` finishes any queued QA run whose handoff was left
  unfinished for `supervisor.qa_handoff_recovery_minutes`.

  Two more states the lifecycle read from the wrong place. Which deploy carries the access is
  decided by the grant record and never by the caller: a handoff repeating after a lost response
  used to propose a fresh run id and deploy under it, while the sweep followed the id the record
  already held — so the untracked deploy could write the identity back after the grant was
  recorded revoked. The proposed id is now only a proposal, everything after the record uses
  `grant.grant_run_id`, and the deploy is published only while the record still says GRANTING and
  no run carries it yet. And a run's terminal outcome is now the first one written: `PATCH
  /api/runs/{id}` refused only the move back to a live status, so an expiring grant's
  `qa_access_expired` failure could be replaced by the QA worker's later `passed` and the story
  supervisor would publish that. A terminal run that already carries a result refuses any rewrite
  of its status, result or error, while a cancelled run with no result may still have one filled
  in — that record is what proves a withdrawn deploy's dispatch is over. Both writers treat the
  refusal as information: the QA consumer drops its stale outcome and keeps consuming, and the
  sweep revokes the access regardless of which reason the run ended up carrying.

  Two things that rule still needed to be true under overlap rather than in sequence.

  An expired dispatch lease was being treated as a fence around the GitHub effect, and it cannot
  be one: the worker reads its own clock and only then starts the `workflow_dispatch` request, so
  it can be paused, or its request delayed, after the check and before GitHub accepts it. The
  sweep would supersede the claim, run a revoke whose fence saw no workflow, and record the grant
  revoked — and the late dispatch would deploy the identity back with no live grant left to remove
  it. What the value actually rides on is the repository secrets, which a workflow reads when it
  runs and not when it is asked for, so a revoke deploy now writes its cleared payload *before* it
  fences. The two together cover every run there can be: one that already read the granted payload
  is on Actions and the fence stops it, and one created afterwards — a worker resuming past its
  lease included — can only read the cleared one. The lease keeps a dead claim from being waited
  on for good, which is all it was ever able to promise.

  What finally closes a grant changed with them, and so did what the system promises. Stopping
  every writer we own is not a criterion we can meet, so the criterion is now that the observed
  state matches the wanted one. `revoked` is written only after the environment of the running
  service has been read back and no longer carries the value; until then the grant stays live in
  `revoking` and the sweep keeps revoking, which is idempotent. The reading goes to `infra-service`
  over the SSH path and the playbooks it already has: `EnvObservationRequest` on the new
  `env-observation:queue`, `ansible/playbooks/observe_service_env.yml` reading the slot out of the
  running containers rather than out of the `.env` file next to them, and the answer left in Redis
  under the request id for the sweep to pick up on a later tick. One question per revoke attempt,
  asked once per `supervisor.temporary_access_observation_window_minutes`. A reading that could not
  be taken — SSH down, playbook failed, nothing running to read — is neither a success nor a
  failure: the grant stays live and the sweep asks again, and past
  `supervisor.temporary_access_unrevoked_ttl_minutes` an unreadable server is reported to admins
  once. A value that shows up on the server behind the sweep's back is a disagreement the next
  cycle sees and revokes again; one that outlives the same TTL or
  `supervisor.temporary_access_max_revoke_attempts` fails the QA run with `qa_cleanup_failed`
  naming what is being observed, so the story goes to a human instead of waiting in TESTING. The
  promise is therefore no longer "the access can never come back" but "the access does not outlive
  one reconciliation interval after it is seen", and the fence, the withdrawal and the supersede
  are what shorten that window rather than what proves it closed.

  Closing the grant used to stop anyone looking at the slot, which put the same hole one step
  later: the writer that can land between two readings can land after the last one, and a value
  applied after the confirmation stayed on the running bot with no live grant to bring it back to
  the sweep. A closed grant is now still read for
  `supervisor.temporary_access_revoked_watch_minutes` — the sweep asks for the live grants plus the
  ones revoked since that cutoff (`GET /api/temporary-access-grants/?live=true&revoked_after=…`) —
  and a reading that finds the value on one puts it back to `revoking` with
  `revoke_reason=observed_after_revoke`, stamps `reopened_at`, and gets a fresh retry budget
  counted from there rather than an exhausted one from hours ago. The clearing deploy goes out on
  the same tick, and admins are told the value came back after it was confirmed gone. The one
  reading that does not reopen a grant is one whose slot has a live owner again: the contract holds
  one value per `(project, env_key)`, so what was read may be the next grant's access, and taking
  it off from here would revoke a grant that is being used.

  That watch is bounded in minutes, and the writer it handles is not. A value restored just past
  the window used to stand for good: nothing read the slot again, so there was no revoke, no
  visible failure and no human. The promise is now made at two speeds. The fast level is the watch
  above, unchanged. Under it the invariant itself — the key is empty while no grant holds it — is
  checked on its own cadence, `supervisor.temporary_access_contract_audit_hours` (24 by default),
  for every `(project, env_key)` slot the record knows, however long ago its last grant closed. The
  sweep asks for it in the same call
  (`GET /api/temporary-access-grants/?live=true&slot_audit_before=…`), which returns one row per
  slot — the newest grant recorded for it — because the slot holds one value and the older grants
  would be the same ssh repeated for history. `observed_at` is what makes a slot due and every
  reading stamps it, so a slot inside the fast watch is never audited on top of being watched, and
  a slot whose server cannot be reached is held to one playbook per interval by a marker the
  scheduler takes before asking. What the slow level finds goes down the existing path: reopened as
  `observed_after_revoke`, revoked, and escalated to a human if it will not go. So the promise no
  longer ends with the window; it becomes slower. The access does not outlive one reconciliation
  interval while the slot is watched, and does not outlive one slow-check interval after that.

  Three things that criterion needed before it was one. `PATCH /api/temporary-access-grants/{id}`
  still accepted `status=revoked` from any internal caller, which is the whole claim the system
  cannot make, so it now refuses it (422) and `POST /api/temporary-access-grants/{id}/observation`
  is the only way to that status: it takes a `TemporaryAccessObservation` and the record decides.
  The reading also has to be of the right machine — applications are unique per
  `(repo_id, server_handle)`, so a project can run on several servers, and reading whichever one
  came back first let an empty slot elsewhere close a grant whose bot still admitted the test
  identity. The observation names its `application_id` and the record refuses one that is not the
  `application_id` in the grant's stored `QAMessage`. And a single empty reading ended
  reconciliation for good, which left a delayed dispatch nothing to be caught by: the grant now
  stays live until `REVOKE_CONFIRMATION_READINGS` readings taken over
  `REVOKE_CONFIRMATION_WINDOW` agree, a reading that finds the value again restarts the streak and
  revokes, and that window is the reconciliation interval the promise is written in. The grant
  record carries the readings (`observed_at`, `observation_id`, `slot_clear_since`,
  `slot_clear_readings`), so a reading delivered twice counts once and the streak survives a
  restart.

  And the run outcome rule was still last-writer-wins: `PATCH /api/runs/{id}` read the row without
  a lock and decided from that stale copy, as did the cleanup escalation. A QA worker's `passed`
  and the sweep's `qa_access_expired` or `qa_cleanup_failed` both read a running run, both passed
  every check, and whichever committed second was what the story read — so a late pass could erase
  the named access failure. Every writer that decides something from what a run already says now
  takes the row first, and the escalation takes its grant and its run the same way.

- The `telegram_bot` service tests bound the whole service directory over `/app`, which made
  Docker materialise the nested `/app/shared` mount point on the host as an empty
  `services/telegram_bot/shared`. That directory shadows the repository's `shared` package for
  anything importing from a service path, so running those tests once left `make test-unit` red
  until it was deleted by hand. Mounted file by file now, like every other service.

- Deploy can now roll out one named commit instead of whatever main holds at dispatch time.
  `workflow_dispatch` only accepts a branch or a tag in `ref`, so a requested `head_sha` is pinned
  by a temporary tag `codegen-deploy-pin-<sha>`, created before the dispatch and dropped on every
  outcome (success, failure, cancellation, unproven cancellation). The finished run's `head_sha`
  is compared with the requested one: a mismatch refuses that deploy with the two SHAs named,
  it no longer passes as success. Reruns stay on the pinned tag. A deploy with no `head_sha`
  behaves exactly as before. `shared/clients/github` gained ref operations (`create_ref`,
  `update_ref`, `delete_ref`, `get_ref_sha`, `create_or_reset_tag`).
  A pin tag that survives cleanup refuses the deploy with the tag named instead of writing a
  successful deployment record, and the tag is created inside the cleanup guard, so an interrupted
  create that GitHub already applied is still removed. The rerun wait polls the deploy's
  cancellation state and stops the rerun it is watching, so teardown leaves no live workflow
  behind: `wait_for_run_completion` takes a `cancel_check`.

- Product analytics are collected again. `LOKI_URL` (the Loki read address, `http://loki:3100`
  inside compose — not the `LOKI_PUSH_*` write credentials) is now a required compose variable with
  no default, and the aggregator no longer treats a missing value as a mode: it raises a
  configuration error instead of sleeping forever. Aggregation also stopped failing on every
  project: the project id reached the API as a `UUID` and the hourly upsert died on it, which left
  `analytics_hourly` empty even with Loki reachable. A service test now drives the whole path
  (real Loki push → query → upsert → rows).
- The LK now tells "no traffic" apart from "nothing was collected". After each completed cycle the
  aggregator records a heartbeat with the projects it failed on; every LK analytics response
  carries a per-project `collection` block (`ok` / `failing` / `stale` / `never` plus the last
  cycle timestamp). A cycle that swallowed errors no longer reports `ok`. The dashboard shows a
  banner and honest empty-state text when collection is down, and per-service status reads
  `unknown` instead of `down` when stale buckets are the aggregator's fault.
- `.env.example` covers everything compose expects without a default: `LK_DOMAIN` and
  `LK_JWT_SECRET` were missing and rode through as empty strings. Both settings now reject an empty
  value, and `docker compose config` runs without unset-variable warnings.

- A failing Time4VPS API no longer hides for a day. The client logs the response body with the
  status for every request and raises `Time4VPSAPIError` carrying it, so `ipnotallowed` (the
  provider's IP allowlist) is no longer read as bad credentials. A sync cycle that could not read
  the provider logs `server_sync_incomplete` at error instead of `server_sync_complete` with zero
  counters, and a repeating failure opens one `provider_api_unavailable` incident plus one admin
  alert, resolved automatically when the provider answers again. Incidents can now be
  platform-level: `incidents.server_handle` is nullable, but only `provider_api_unavailable` may
  omit it. Every other type stays server-bound, so its `(server_handle, incident_type)` active
  unique index keeps deduplicating.

- `scripts/system_configs.yaml` is now the source of truth for the keys it declares. Deploy applies
  it after migrations and overwrites existing values, printing one line per diverged key with the
  database and file values. A value edited through the API survives until the next deploy; a key
  absent from the file is still owned by the database alone and is never touched. This closes the
  two ways the file and the database drifted apart: an edited value that never arrived, and a newly
  required key that only failed at the next scheduler restart.
- ConfigStore now separates "the config source is unavailable" from "the key does not exist". An
  unreachable API, an HTTP error, or a broken response body falls back to the last known value with
  a warning, so working loops keep running while the API restarts. A 404 is still a `KeyError` and a
  missing required key still fails service startup.

## 2026-07-27

- Telegram bot audiences now come from service-template 0.3.6's environment contract. The PO
  records an explicit `bot_access` selection and deploy-time
  `TG_BOT_ALLOWED_TELEGRAM_IDS` literal for private, public and custom audiences;
  private projects with an empty audience fail before deploy. Existing
  `ADMIN_TELEGRAM_ID` secrets are mapped to the contract audience when the repository declares
  it. Existing pre-0.3.6 repositories retain their legacy access behavior. The template enforces
  the configured audience before application handlers, so the PO menu does not offer an
  administrator-managed expansion until an access-contract extension exists.

- Production scaffolding is pinned to service-template `0.3.6`, the release that makes bot access
   a contract instead of code an engineering agent writes per project. The template declares
   `TG_BOT_ALLOWED_TELEGRAM_IDS` for the audience and `TG_BOT_TEST_TELEGRAM_ID` for one temporary
   identity that makes a private bot testable; removing the value revokes it and leaves no state
   behind. The Stage 5 compatibility harness was run against the candidate release before the pin
   moved, as the production template rule requires. Filling the audience from the PO access menu
   and driving the temporary identity around a QA run were tracked separately
   (`codegen_orchestrator-826`, `codegen_orchestrator-744`). The audience menu is now implemented;
   temporary-identity lifecycle remains in `codegen_orchestrator-744`.

- Grafana now provisions a read-only PostgreSQL datasource and repository-owned server-capacity
  and run-operations dashboards. Server history panels were checked against 508 real snapshots;
  run panels use an isolated disposable database because the live `runs` and `task_events` tables
  are empty. The verification report records the query timings and seed coverage.

- Provisioner playbooks now use the repository Ansible configuration without loading the removed
  `yaml` callback. This restores role resolution for both `deploy_target` during new-server
  provisioning and `monitoring` on existing servers.

- Removed the unused Langfuse receiver stack and its ClickHouse and MinIO dependencies. The Langfuse SDK had been the only source of OpenTelemetry packages; the application has no independent OpenTelemetry instrumentation, so those packages were removed with it. This corrects the task's premise that such instrumentation existed. Loki, Promtail, Grafana, and product analytics are unchanged. The 2026-07-26 production measurement recorded 995 MB RSS and 4.6 GB of images across the removed services; an operator can reclaim those resources after deployment by removing the obsolete containers, images, and volumes. The admin user's Messages view was removed with the unavailable trace data.

## 2026-07-26

### Changed
- Split backend integration coverage into the required Compose-only `backend` suite and the manual
  `backend-dind` suite. Worker-container coverage remains in `tests/live/` until secretary-774
  provides host-side gates with a real Docker socket.

## 2026-07-26

- `make test-unit` now points `API_BASE_URL` at an unreachable loopback port. It can no longer
  read system configuration from a developer's running API while CI has no API, and the CI
  contract rejects a return to a host-service URL.

- Repeated QA failures now leave their fingerprint and attempt evidence on the
  story held for human review. PO checks that hold before creating another
  story or publishing architect work, so a reminder cannot restart the same
  project-level repair loop.

- QA now distinguishes a product failure from an inability to test. A typed
  `blocked` outcome carries a closed-category blocker with the attempted action
  and request/response evidence. Platform-owned preflight checks cover the
  deployed URL, QA server, Claude Code, Telegram identity and Telethon
  credentials. Telegram runs send `/start` as the QA identity before Claude is
  invoked; an access denial, or an unknown preflight classification, parks the
  story in `waiting_human_review` and never creates a fix task. This prevents a
  private bot from being changed because the QA account lacked access.

## 2026-07-25

- A stop or undeploy now names the application it acts on. `DeployMessage` carries
  `application_id` and rejects a lifecycle action without one; the consumer reads the target's
  server from it and skips allocation entirely. Before, it asked the allocator, which answers with
  one application on the project's primary repository whatever the message said — so a project
  deployed on two servers got the same container brought down twice, the other one stayed up with
  its bot still polling, and its application sat in `undeploying` forever while the teardown kept
  reporting `pending`. For the same reason the undeploy path no longer releases the bot on the
  first application to report `not_deployed`: the release waits until the project has nothing left
  running, because the row does not say which server the bot is on. A run cancelled on the project's
  deploy lock now counts as a stalled teardown, so asking again sends that application down instead
  of leaving it stranded.

- PO can now tear a user's project down, which is what makes "your own project holds that token" a
  choice instead of a dead end. `teardown_project` calls `POST /api/projects/{id}/teardown`: the
  endpoint checks the caller owns the project and publishes an undeploy for every application of its
  repositories that is still up. It does not free the bot there and then. A running bot long-polls
  its token, and Telegram answers 409 to whoever binds that token second, so a teardown that
  promised a free token before `deploy_lifecycle` had run `compose down -v` would hand the user a
  rebind that fails. Instead the POST comes back `pending`, and `GET` on the same path reports where
  the teardown stands — archiving the project and releasing the binding only once every application
  reads `not_deployed`, and reporting `failed` if the undeploy run failed rather than waiting
  forever. The tool polls that until it settles, so the agent learns the bot is free at the moment
  it is. `validate_telegram_token` now passes `conflict_project_id` through to the agent, and the
  prompt tells it to offer the two real options, continue in the holding project or free the token
  and rebind after the teardown confirms, and never to tear anything down unasked. The owner check
  lives on the new endpoints because the per-application `stop`/`undeploy` endpoints have no
  authorization at all (backlog #1022) and this route is driven by a user, not an admin: someone
  else's project comes back 403, untouched and still holding its bot.

- Teardown now hands the bot back. The uniqueness check added the same day would otherwise lock a
  token to a dead project forever: the binding lives on `Repository.bot_username`, and nothing ever
  cleared it, so a user could not reuse their own bot after tearing the project down. Two
  transitions release it, both keyed on the resulting state rather than on the request: a project
  going `archived`, and an application landing in `not_deployed`, which is where the undeploy
  consumer reports back to. Release clears `bot_username` on the repositories concerned and drops
  `TELEGRAM_BOT_TOKEN` / `TELEGRAM_BOT_USERNAME` from the project's secrets, in the caller's
  transaction. It is idempotent: a second archive or a redelivered patch finds nothing and changes
  nothing. Stop keeps the binding, since a stopped application is one redeploy from running again,
  and freeing the token while the bot may still be polling would only recreate the 409 the
  uniqueness check exists to prevent. Deleting a project takes the third route: `DELETE
  /api/projects/{id}` now clears the rows that reference it, repositories among them, so the
  binding disappears with the project. It could not before — none of those foreign keys cascade in
  the database, and the endpoint left repositories, tasks, stories, analytics and RAG rows behind,
  so deleting any real project failed on its final commit and the bot stayed held by a project the
  user had asked to be rid of.

- Token validation now refuses a bot that another live project already holds. Until now there was
  no uniqueness check at all: the palindrome bot `196ba936` and the echo bot `b380adb4` both took
  `@factory_e2e_test_bot`, and the clash only surfaced later as a 409 from Telegram. The last layer
  in the chain looks the bot up by `Repository.bot_username` across projects that are not archived,
  and answers by owner. The same project re-sending its own token is an iteration and passes.
  Another project of the same user is named in the message, with `conflict_project_id` on the
  verdict so PO can offer to continue there (`bound_to_own_project`). Someone else's project gets a
  refusal that describes nothing — not the project, not its id, not its owner (`bound_elsewhere`).
  A user who somehow holds the bot in their own project while a stranger holds it too gets the
  generic refusal: sending them to their own project would walk them into the same clash. The
  lookup and the owner comparison happen server-side; `validate_telegram_token` now takes the
  session and the target project.

- Token validation now catches a bot already running on the token outside our system, the case
  where a user started it at home and forgot. Two layers after `getMe`: `getWebhookInfo` (read-only,
  a non-empty `url` means someone wired a webhook up) and a `getUpdates` probe with no `offset`,
  `limit=1`, `timeout=0` and a 5s deadline, where a 409 means another poller holds the token. QA saw
  exactly that on a reused token, `409 Conflict` every ~34s. The probe confirms nothing and
  consumes nothing: an update is confirmed only when `getUpdates` is called with an offset above its
  `update_id`, and a negative offset makes earlier updates forgotten, so the probe sends no offset
  at all. With no server-side wait another bot's poll loop misses at most one cycle. Both rejections carry the same
  generic message — something is running on this token, stop it or send another — because we know
  a bot answers, not whose it is. Reason codes: `webhook_active`, `poller_active`. Webhook is
  checked first, since a set webhook makes `getUpdates` answer 409 for that reason alone. The
  poller probe only ever proves activity, never its absence: an idle bot passes, and any answer
  other than 409 is logged, not treated as a refusal.

- Telegram token validation became a server-side step behind one door. `POST
  /api/projects/{id}/telegram/token` runs the check chain (format, then `getMe`) and returns a
  typed `TelegramTokenVerdict` — `ok`/`rejected`, a `reason_code`, a per-layer `checks` list and a
  message safe to show the user. Secrets and `Repository.bot_username` are written in one
  transaction, and only on a passing verdict; a project without a primary repository gets 409
  rather than a half-bound token. The generic secrets endpoint now refuses anything keyed
  `TELEGRAM_BOT_TOKEN` or shaped like a bot token with 422, so the only path in is the validator —
  the gate no longer rests on a sentence in the PO prompt telling the model which tool to prefer.
  PO's `validate_telegram_token` is a thin caller that voices the verdict; the getMe call, the
  username write and the retry wording left the agent. Later check layers (uniqueness, external
  poller detection) append to `checks` without touching the prompt.

  Whole-config writes are fenced the same way: `POST /api/projects/`, `PUT` and `PATCH` scan the
  submitted config tree for `TELEGRAM_BOT_TOKEN` keys and token-shaped values, and `config.secrets`
  is no longer writable through them — the stored blob is carried over and a caller sending a
  different one gets 422. Scaffolder's read-modify-write of the whole config still works, since it
  hands back the blob it read.

- Load the QA server's Telethon credentials into the run instead of asking the agent to. The
  prompt told it to `set -a; . ~/.qa-telethon.env`, which run `qa-7729960c` simply skipped: it
  reported the echo check `BLOCKED`, citing the old `/opt/qa-runner/telethon.session` path and
  variable names that no longer exist, while the credentials sat on the server at 0600. The
  runner now sources the file into the SSH command's environment next to the `PATH` export, so
  `TELETHON_*` are set before `claude` starts, and the prompt says they are already exported.
  A bot run first checks the file is readable and all three variables are non-empty; if not, the
  run fails with the named cause and `claude` never starts, so there is no room for a guessed
  verdict. Only variable names travel back in that error, never values. The prompt also drops
  "blocked" as an allowed outcome for Telegram checks: pass or fail, decided by running the
  snippet.

- Wire Telethon credentials through to the QA runner, so QA can write to deployed bots as a real
  user. The prompt hardcoded `TelegramClient('/opt/qa-runner/telethon.session', api_id=0,
  api_hash='')` and the role copied that session file only `when: telethon_session_file is
  defined` — a variable nobody set, so the copy skipped silently and every bot check ended in
  `BLOCKED: /opt/qa-runner/telethon.session does not exist`. api_id/api_hash of 0/`''` would not
  have connected either: Telethon needs the app credentials even with an authorized session. The
  role now reads `TELETHON_API_ID`, `TELETHON_API_HASH` and `TELETHON_SESSION` from the
  orchestrator environment (never through `--extra-vars`, which would put them in the process
  list), asserts all three are non-empty, and writes them to `~/.qa-telethon.env` at mode 0600
  owned by the QA user. The prompt sources that file and connects through `StringSession`.
  Verified on vps-273978: a missing credential fails the play with a named message, and a QA run
  against `@factory_e2e_test_bot` echoed back its message on every check.

## 2026-07-24

- Install the QA runner's Claude Code for the user QA actually connects as. The `qa_runner` role
  installed under root (`HOME=/root`, gate on `/root/.local/bin/claude`, PATH line in
  `/root/.bashrc`), while QA SSHes in as the server's `ssh_user` and runs a bare `claude` off
  `$HOME/.local/bin` — every run on `vps-273978` came back as `Claude Code exited with status 127`.
  The install now runs as `{{ deploy_user }}` with that user's `HOME`, and the credentials and
  settings land in the same home. The bashrc line is gone: a non-interactive SSH session never
  reads it, QA sets its own PATH. Second defect in the same task: `curl … | bash` ran without
  `pipefail` under `/bin/sh`, so a failed download left bash reading empty input and exiting 0 —
  Ansible reported a successful install of a binary that was never there. The command now runs
  under `/bin/bash` with `set -euo pipefail`, and a following task executes `claude --version` as
  the QA user, so the role proves the binary works instead of trusting that a file exists.

- Make deploy smoke verify the Telegram bot instead of skipping it. The tg_bot check ran through
  Telethon, whose API id, hash and session were never configured on the deploy worker, so every
  bot project logged `smoke_tg_bot_skip` and the deploy passed with only the backend verified,
  leaving the bot itself to be checked by hand. The check now calls Bot API `getMe` with the
  project's token and confirms over SSH that `docker compose ps` lists the `tg_bot` container as
  running. Both probes are mandatory: a missing token, a missing server handle, an unreachable SSH
  or an unallocated module is a `fail` naming the reason, not a silent skip, and the resulting
  `bot_username` still reaches QA. Telethon is gone from the deploy worker (dependency, env vars
  and session mount); the QA runner keeps its own installation for dialogue testing.

- Give QA the bot username from the project instead of the deploy smoke check. PO's
  `validate_telegram_token` already knew the username from `getMe`, but its write to
  `Repository.bot_username` skipped itself whenever the repository lookup returned anything
  unexpected, and the supervisor read the username only off `DeployRunResult`, which the smoke
  check leaves `null`. A tg_bot project then failed QA with `qa_bot_username_missing` on a working
  bot, and the story's `failed` status sent PO off fixing a product that was fine. The PO tool now
  raises when it cannot store the username, and the deploy→QA handoff reads it from the same
  repository record it reads the acceptance criteria from, falling back to the smoke value only for
  projects whose token predates the write.

- Make missing agent LLM config visible. `ARCHITECT_LLM_*` was absent from `.env.example`, so a
  clean install had the architect consumer accept stories and fail each one at
  `architect_llm_not_configured` with nothing said at startup; PO logged its disabled state at
  info. Both groups are now documented in `.env.example`, the architect service refuses to start
  without them, PO logs `po_consumer_disabled` at error with the missing var names, and
  `src/config/agent_llm_env.py` holds the groups so a unit test can check `.env.example` and
  `Settings` against each other.

- Stop project git hooks from gating worker pushes. Generated projects ship
  `.githooks/pre-push`, which falls back to `make lint` when Docker is absent and resolves it to
  `.venv/bin/ruff`; neither exists in a worker container, so the hook exited 127 under
  `set -euo pipefail` and every push carrying real file changes was rejected. The agent's commits
  never left the container while it still reported success, leaving an empty story branch.
  `setup_git_repo` now points `core.hooksPath` at an empty directory outside `/workspace`; PR CI
  remains the gate. The noop path never saw this because an empty commit makes `pre-push`
  short-circuit before any check.
- Fail engineering when the reported commit is not on origin. `ReposMixin.branch_contains_commit`
  resolves a SHA against the remote branch, and the developer node now verifies the worker's
  self-reported SHA before reporting success instead of only checking that the string is non-empty.
- Distinguish GitHub's 422 rejections when opening a story PR. `create_pull_request` parses the
  validation body: "No commits between" now raises a message naming the empty branch, instead of
  being treated as "PR already exists" and surfacing as `no existing PR found`.
- Bound ClickHouse log growth. `docker/clickhouse/config.d/retention.xml` sets logger level
  `information` with 200M x 3 rotation and a TTL on the six `system.*_log` tables, which had none
  and grew without limit.

## 2026-07-20

- Make deploy port selection role-based. Deploy allocation and the live harness now share the same
  port service metadata, and deployed URLs are selected from HTTP-health-serving module ports
  instead of whichever application allocation the API returns first.
- Fix standalone live-test sweeping after the project slug migration. `clean_live_tests.py` now
  selects projects by `title`, carries `slug` into GitHub and remote cleanup, and fetches remote
  server SSH users and decrypted keys through the authenticated internal API instead of querying
  removed or encrypted DB columns. Broken server/key API reads now fail the sweep before database
  deletion, so retries can still recover DB-derived slugs instead of silently skipping remote
  service directories.
- Stop inferring live-work teardown settlement from consumer `status` strings. Consumers now mark
  their own results as settled or unsettled for the live-work fence, so QA `passed`, duplicate
  `skipped`, architect skips and engineering `gave_up` can ACK during teardown while deploy
  failures and unmarked new outcomes still fail closed.
- Resolve the default-branch head SHA before admin-triggered deploy creation. Admin redeploy and
  create-from-repo now publish deploy messages with a concrete `head_sha`, and fail the API request
  before Redis publication when GitHub cannot provide one.
- Fence result-shaped live deploy failures during teardown. Active live work now checks the cancel
  marker after `process()` returns and treats failed, cancelled or error-shaped results as
  unproven settlement: the stream entry stays pending and `live:work:failed` blocks cleanup, while
  explicit success results can still ACK.
- Require an exact `head_sha` before loading a deploy environment contract. Missing or empty commit
  SHA now returns the typed `head_sha_missing` deploy outcome instead of reading from `main`, while
  engineering-triggered deploys pass the completed commit SHA into the deploy message. Deploy retry
  runs now preserve the original merged SHA in run metadata and on the retried deploy message. The
  deploy worker now rejects missing commit SHAs before allocation, SSH precheck, repository lookup
  or subgraph execution.
- Align local `make lint` with CI fast checks by running Ruff format verification before Ruff lint.
  `make ci-contract` now also rejects future drift between the CI Ruff steps and the Makefile
  `lint` recipe.

## 2026-07-19

- Route runtime project identity through immutable `project.slug` in deploy,
  repository setup, allocation, DevOps secrets, smoke logs and QA. Deploy now
  stores `Application.service_name` as the slug, QA uses the same slugged
  server directory, and SSH command paths/compose names are shell-quoted at the
  command boundary.

## 2026-07-18

- Split project display titles from runtime slugs. Projects now store free-text
  `title` plus immutable server-generated `slug`, with a unique indexed DB column,
  API create/update rejection for client-provided slug changes, and slug
  backfill in migration `c7d8e9f0a1b2`.
- Run runnable offline `tests/live/` regressions as one discovered suite in CI and local
  `make test-unit`. `make test-live` now uses a single directory pytest command with explicit
  ignores for stack-dependent, Redis-only and historical expected-RED live files, while
  `make ci-contract` rejects a return to per-file CI enumeration or a missing local live suite.
- Add OpenAI Codex CLI as a developer-worker type alongside Claude Code and
  Factory Droid. Project config now routes `agent_type=codex` without changing
  the worker queue envelope, and unknown values fail explicitly. The dedicated
  `worker-base-codex` image pins Codex CLI 0.144.6 and runs as the existing
  non-root worker with the same workspace and resource limits. Host-session
  mode validates and mounts an isolated file-backed `HOST_CODEX_HOME` read-write
  for token refresh, without requiring an API key or defaulting to `~/.codex`.
  Production deployment persists that host profile path and pulls the Codex
  base image alongside the common, Claude, and Factory images.
  Codex runs non-interactively with workspace-only writes and reports through
  the existing HTTP bridge; sandboxed network commands are enabled for the
  bridge, dependency access, and Git push. Its stdout and stderr are not
  persisted as results. Optional API-key mode uses `CODEX_API_KEY`.
- Make live harness API reads fail loud before body parsing. Parsed `tests/live` API responses now
  call `raise_for_status()` first, including scaffold/project polling and auth-gated server,
  ssh-key and port-allocation checks, with a mock-transport regression for a rejected polling call.
- Route live teardown run discovery through an internal-only API client. Cleanup now sees unowned
  deploy and QA runs, selects only records whose `project_id` matches the teardown manifest,
  cancels active records, and proves their terminal status before deleting external resources.
- Extend the live mega pipeline with a real Claude developer worker variant. The noop route still
  proves the deterministic plumbing, while the LLM route now scaffolds a backend-only project,
  runs engineering with a longer timeout, verifies the merged env contract has no user-secret
  entries, then gates on deploy success, `/health` 200, and non-LLM QA passed. Real LLM worker
  containers now get a 4GB memory limit; noop workers keep the smaller 2GB limit. Factory worker
  startup was also hardened to forward its API key and request Droid's non-interactive edit mode.

## 2026-07-16

- Give post-deploy QA one source of truth for acceptance criteria. `Repository.acceptance_criteria`
  is seeded with a baseline health check at repository creation, so an automatically created story
  reaches QA without anyone filling the repository in by hand. The deploy → QA handoff resolves the
  criteria and carries them on `QAMessage`; missing criteria now fail the story visibly before it
  reaches TESTING (and answer 422 on admin `run-e2e`) instead of leaving a QA run that can only
  error with `qa_no_acceptance_criteria`. Criteria stating only `GET <path> returns <status>` are
  checked over HTTP by the QA consumer, so a health-only story completes with outcome `passed` and
  no LLM. The mega now gates on the QA run the pipeline itself produced rather than its own health
  request.
- Fix live server cleanup for service-template compose names that normalize project slugs with
  underscores. Teardown now discovers actual `com.docker.compose.project` labels from live
  containers, runs compose down for both manifest and discovered names, removes by label and
  container-name prefix, and fails if either hyphenated or underscored residue remains.

## 2026-07-15

- Resolve service-template 0.3.1 PostgreSQL and Redis host ports from persisted application
  allocations. Deploy now requests the infrastructure port services through the atomic allocation
  API, fills only missing services on redeploy, and fails visibly on missing or ambiguous mappings.
- Persist structured failed GitHub Actions job and step evidence on CI fix tasks, deduplicate runs,
  fingerprint repeated failures across commits, and bound identical fixes before routing the story
  to human review with one admin alert. A transient story-transition failure remains retryable on
  the next scheduler cycle. Live debug artifacts retain the evidence through cleanup.
- Made owned-worker teardown idempotent across scheduler and live cleanup races: concurrent Docker
  removal is accepted only after bounded absence verification, operational failures remain visible,
  and live cleanup deletes and verifies worker Redis keys independently.
- Hardened the Stage 7 live mega path: noop engineering pushes the checked-out story branch and
  reports git failures through the worker result API, Redis blocking-read timeouts are idle polls,
  the public registry hostname resolves to Caddy inside Compose, and owned worker cleanup plus
  `test-live-clean` residue verification are fail-closed.
- Closed the post-merge live cleanup gaps: recover crash manifests before broad cleanup, qualify
  joined SQL predicates, discover worker containers by ownership label, reconnect idle pubsub
  listeners, and keep raw git output out of noop failure results.

Format: [Keep a Changelog](https://keepachangelog.com/). Grouped by date.

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
- **Bound worker compose and incident-journal failures (`codegen_orchestrator-493`)**: generated worker-mode compose recipes now preserve transport, JSON and compose exit failures, while required Makefile proxy installation fails the worker task. Provisioner journal outages retry only the journal write after external provisioning has run; a bounded retry budget publishes one terminal provisioning result before ACK, preventing both infinite PEL reclaim and repeated provisioning side effects.

## 2026-07-13

### Changed
- **Typed engineering consume (Sprint 002 Phase 3, `codegen_orchestrator-457`)**: `process_engineering_job` now validates its input with `EngineeringMessage.model_validate(job_data)` before any business logic, mirroring the deploy/qa/architect consumers. Removed the hand-unpacking of 11 fields via `job_data.get(...)` and the fallback defaults for required message data (`task_id`/`user_id`/`action`/`skip_deploy`/`deploy_fix_attempt`), so a malformed job no longer runs with `"unknown"`/`""`/`"create"` placeholders. A `ValidationError` is handled as a terminal poison entry, not a raise: `_handle_invalid_engineering_message` logs only `type`+`loc` (never the raw payload — it carries the user's `description` and ids) and fails the run when `task_id` is present. The entry is ACKed only once that terminal outcome is durably written: if `_fail_job` hits a transient API error (5xx or a transport error) the handler re-raises so the `auto_ack=False`/`claim_pending` loop leaves the entry unacked and retries after the API recovers; only a non-retryable client error (e.g. 404 — no such run) is ACKed, to avoid an eternal poison-loop. `action` comparisons use `ActionType.CREATE`. Tests: `services/langgraph/tests/unit/consumers/test_engineering_validation.py`.

### Removed
- **Dead `langgraph/src/tools/` layer (B5, `codegen_orchestrator-457`)**: deleted `tools/{projects,servers,github,specs}.py`, `tools/__init__.py`, `tools/base.py`, and the dead result models in `schemas/tools.py` (~800 lines that shadowed the live agent tools and eager-imported `ddgs`/`yaml`/github deps). The only live piece, `allocator.py`, moved to `services/langgraph/src/allocations.py`; `nodes/resource_allocator.py` and `consumers/deploy.py` import from there, and `schemas/__init__.py` no longer re-exports the tool models. Guard test: `services/langgraph/tests/unit/test_dead_layer_removed.py`.
- **`worker:lifecycle` stream + contract (`codegen_orchestrator-457`)**: no consumer existed. Removed `WorkerWrapper.publish_lifecycle` and its 3 call sites, the `WorkerLifecycleEvent` contract (`shared/contracts/queues/worker_lifecycle.py`), the `WorkerChannels.LIFECYCLE` member, and the now-unused `WorkerLifecycleKind` vocab slice.
- **Second `agent_config_cache` (`codegen_orchestrator-457`)**: deleted `services/langgraph/src/config/agent_config_cache.py`, a redundant TTL cache stacked on `agent_config.get_agent_config` (which already caches). `nodes/base.py` and `subgraphs/devops/env_analyzer.py` call `get_agent_config` directly.
- **Unreferenced `worker-manager/src/scaffold_phase.py` (`codegen_orchestrator-457`)**: legacy scaffold path with no production callers; removed with its unit test.
- **`shared` compat-shims (`codegen_orchestrator-457`)**: removed the `shared/__init__.py` `try/except → RedisStreamClient = None` swallow (now a direct fail-fast import), the `ServiceDeployment`/`DeploymentStatus` aliases and the legacy `DeploymentStatus` enum in `shared/models`, and the `ensure_consumer_groups` alias in `shared/queues.py`. Guard test: `shared/tests/unit/test_phase3_shims_removed.py`.
- Note: raw `publish`/`publish_flat` on `RedisStreamClient` stay public — ~13 live production producers still call them; the raw API was not extended and per-consumer migration to `publish_message` continues in Phase 3/4.

## 2026-07-12

### Changed
- **Typed `Run.result` (Sprint 002 Phase 2 keystone, `codegen_orchestrator-440`)**: `RunDTO.result` is no longer `dict | None`. Added `shared/contracts/dto/run_result.py` with one `extra="forbid"` model per `RunType` — `EngineeringRunResult` (`engineering_status` + commit/modules/tests), `DeployRunResult` (`deploy_outcome` + deployed_url/application_id/bot_username/deploy_fix_attempt/error_details/action + opaque deployment/smoke blobs), `QARunResult` (`qa_outcome` + summary/`failed_checks: list[QAFailedCheck]`/report/qa_attempt/deployed_url/error). `RunDTO` binds the union to `type` (`_check_result_matches_type`): a payload of the wrong type, an unknown field, an unknown outcome, or a missing required field is rejected at the boundary; `result=None` stays valid for runs with no result yet. Producers (`deploy`, `deploy_result_handler`, `deploy_failure_handler`, `engineering_result_handler`, `qa`) now build the typed model and `model_dump(mode="json")` — one wire form, and the duplicated inline classification→outcome dict in `deploy_result_handler` is replaced by the shared `_classification_to_outcome`. The scheduler supervisor reads `run.result.deploy_outcome` / `.qa_outcome` / typed attributes instead of `run.result or {}` + `.get()` + `DeployOutcome(str)`. `result=None` is allowed only before the outcome exists (`QUEUED`/`RUNNING`/`CANCELLED`); a `COMPLETED`/`FAILED` run without a result is rejected, so a terminal run that lost its outcome surfaces loudly instead of wedging a story forever. All producer failure paths that reach a terminal status now write a typed result — deploy outcomes, `QAOutcome.ERROR` when QA can't resolve its server (previously the run stayed `QUEUED` and the story sat in `TESTING` forever), and `EngineeringStatus.FAILED`/`GAVE_UP` on engineering failure/give-up paths. Migration: `Run.result` stays a JSON column and the API's `RunRead` stays dict-typed (passthrough), so no DB migration and no break on historical reads; in-flight runs parse by construction. The scheduler validates only the latest run per story (`get_latest_run_by_story` parses `rows[0]` alone), so an older legacy/corrupt run can't fail a story whose current run is valid; a latest run that fails validation (wrong-type/corrupt, or terminal-without-result) is routed to a terminal, visible state (`supervisor._fail_story_on_invalid_result`: fail story once + notify admins) instead of poison-looping. `deployed_url`/`application_id` stay optional on `DeployRunResult` (a standalone deploy, or a success where the app record didn't resolve, legitimately lacks `application_id`), but the QA handoff needs both — so `_handle_deploy_success_story` validates them *before* transitioning the story or creating a QA run, routing a `SUCCESS` that can't reach QA to a visible failure instead of crashing the tick after partial state. Contract tests in `shared/tests/unit/test_run_result.py` (each type, `result=None` per non-terminal status, terminal-without-result rejection, cross-type/unknown-outcome/unknown-field/missing-field rejection, optional-field round-trip); scheduler routing/invalid-result/CANCELLED-skip/terminal-no-result/missing-handoff-fields tests split across `services/scheduler/tests/unit/test_supervisor.py` and `test_supervisor_run_routing.py` (shared factories in `_run_routing_factories.py`); latest-only validation tests in `test_api_client.py`; QA server-resolve terminal-result test in `test_qa_consumer.py`. Closes the last slice of Phase 2; next is Phase 3 typed Redis consume.
- **Unified contract vocabularies (Sprint 002 Phase 2, `codegen_orchestrator-436`)**: added `shared/contracts/vocab.py` with one canonical `StrEnum` per cross-service concept — `AgentType` (moved here, re-exported from `queues.worker`), `ActionType`, `ResultStatus`, `LifecycleEvent`. Replaced the competing inline `Literal[...]` sets: `BaseResult.status` (dropped the `error` failure synonym, now `success`/`failed`/`timeout`), `EngineeringMessage.action` (`ActionType`), and `AgentConfigDTO.type` (`AgentType`). `LifecycleEvent` is the canonical member set, but each wire keeps its own supported slice as an explicit `Literal` subset over the enum members — `TaskProgressKind` (`started`/`progress`/`completed`/`failed`) for `ProgressEvent.type` and `WorkerEvent.event_type`, `WorkerLifecycleKind` (`started`/`completed`/`failed`/`stopped`) for `WorkerLifecycleEvent.event` — so the historically different per-field vocabularies are preserved, not merged (a progress stream still rejects `stopped`, the worker-lifecycle stream still rejects `progress`). Worker-side comparisons now use the enum instead of raw `"claude"`/`"factory"` strings (`worker-wrapper` config + wrapper, `worker-manager` container_config/manager/`_get_agent`), and `worker-manager` consumer stops passing `agent_type.value`. The provisioner-result / infra-service producer/consumer and the telegram admin notifier use `ResultStatus`. Because `BaseResult.status` no longer accepts `error`, the scheduler `provisioner:results` consumer now treats a message that fails validation as terminal (`handle_provisioner_entry`: logs it and ACKs) instead of leaving it unacked to poison-loop the `claim_pending` reclaim; transient processing errors still stay unacked for retry. Two historically-mismatched vocabularies are kept distinct on purpose, not merged: `DeployAction` (adds `stop`/`undeploy`) and `TaskType` (adds `refactor`) stay separate from `ActionType`, while `WorkerEvent.worker_type` remains the separate `WorkerCliKind` vocabulary (`droid`/`claude_code`/`codex`). Contract tests in `shared/tests/unit/test_vocab.py` (accepted values + rejection of unknowns and of out-of-slice lifecycle values per field) and `services/scheduler/tests/unit/test_provisioner_entry.py` (poison message ACKed, valid processed+ACKed, transient error left unacked). Out of scope (still open in Phase 2/3): `Run.result` union, typed Redis consume / raw-dict consumers.
- **Typed response-DTO lifecycle fields (B7 slice of Sprint 002 Phase 2)**: lifecycle fields on the read-side DTOs now declare their existing `StrEnum` instead of bare `str`, so Pydantic rejects unknown values at the read boundary. `TaskDTO.status/type` and `TaskEventDTO.event_type/from_status/to_status`, `StoryDTO.status/type`, `ServerDTO.status` (+ `ServerCreate.status`), `ApplicationDTO/Create/Update.status`, `IncidentDTO.incident_type/status` (+ `IncidentCreate.incident_type`, `IncidentUpdate.status`), and `ServiceDeploymentDTO.status` (`DeploymentResult`). Dropped the "use str for flexibility" comments. Added accept-valid / reject-unknown unit tests per DTO. Only the B7 response-DTO slice; the duplicated vocabularies and `Run.result` typing from Phase 2 remain open.

### Fixed
- **Worker-mode compose proxy targets**: worker-wrapper now overrides service-template's portless `worker-start` and `worker-stop` targets instead of local-mode `dev-start` and `dev-stop`. Start preserves service filtering and sends `up -d --build --wait`; stop remains project-scoped and does not remove volumes.
- **Pinned production scaffolding**: both scaffold paths now use the typed GitHub source `gh:vladmesh/service-template` and an explicit system-config ref, baseline `0.3.0`. Removed the unused local template mount, reject floating refs, and record Copier's resolved commit for reproduction.

## 2026-07-11

### Changed
- **Normalize CI merge gate**: added stable `Required CI Gate` job, mandatory CI contract check, unconditional format/lint/unit checks, expanded service and integration routing for shared/test/dependency/workflow changes, and explicit assertions that required matrix test commands actually ran before a matrix job can pass.

## 2026-07-10

### Fixed
- **API service tests after internal auth hardening**: service-test compose now provides `INTERNAL_API_KEY` to both the API container and test runner, and API service test clients send `X-Internal-Key` by default. This keeps server/project/run test setup aligned with the fail-closed internal auth contract. `make test-service SERVICE=api` now also checks compose container exit codes before cleanup, so dependency startup failures cannot be reported as a green test run.

## 2026-05-29

### Changed
- **Upgrade redis-py to 8.0.0 + make the consumer layer compatible**: bumped `redis[hiredis]` to `>=8.0.0` across all services and pinned `redis==8.0.0` in the `requirements.lock` files. redis-py 8 stopped applying `decode_responses=True` to the field maps returned by `XREADGROUP` / `XREAD` / `XAUTOCLAIM` and `XINFO GROUPS`, and to `HGETALL` (they now come back as `bytes`), while `XRANGE` / `HGET` / `SMEMBERS` etc. still decode. Added `decode_redis_value` / `decode_redis_fields` helpers in `shared/redis` and normalized every affected read: `RedisStreamClient.consume`/`_recover_pending`, PO consumer (`po.py`), telegram-bot `XREAD`, API debug router (`XINFO GROUPS`), and worker-manager `HGETALL` sites (`manager.py`, `introspect.py`, `garbage_collector.py`). Also hardened `consume()` to cede the event loop on empty reads (fakeredis ignores the block timeout, which would otherwise busy-spin and starve the loop). Without this the entire consumer layer silently broke on redis 8 (messages dropped on validation, JSON never unwrapped). Tests updated to read through the decode boundary; added a `_parse_fields` bytes-decoding regression test.
- **QA tester prompt moved into `prompts/` package**: extracted `build_qa_prompt` from `services/langgraph/src/consumers/_qa_runner.py` into a dedicated `services/langgraph/src/prompts/qa/__init__.py`, consistent with the `architect`/`po`/`developer_worker` prompts. `_qa_runner.py` now imports the builder. No behavior change (prompt text preserved verbatim).

## 2026-04-09

### Added
- **Stale queue message cleanup** (#1021): Centralized staleness guard in `_base.py` — before processing, consumers check if the referenced run/story is terminal (COMPLETED/FAILED/CANCELLED/ARCHIVED). Stale messages are ACKed and skipped instantly, preventing the 75-message flood that blocked the 2026-03-13 escort for hours. New `queue_cleanup_worker` in scheduler runs every 10 minutes: cleans orphan `po:response:*` and `worker:*:input/output` streams idle >10min, trims entries >7 days from all task queues via XTRIM MINID. Architect's duplicate guard simplified to DEPLOYING only. 20 unit tests.

## 2026-03-21

### Added
- **Personal-area frontend SPA** (#1036): New `services/user-dashboard/` — React + Vite + Tailwind CSS + Recharts SPA for non-technical founders. Auth flow (one-time token → JWT), project list with summary metrics, project dashboard with period selector (24h/7d/30d), 4 KPI cards, line chart with metric switching, service status, top endpoints, per-service breakdown. Docker: node build → nginx:alpine, port 3003. Light theme, Russian UI, mobile-responsive.
- **LK API: auth + analytics endpoints** (#1034): JWT auth flow via `POST /api/lk/auth/token` (one-time Redis token → 24h JWT). 4 owner-scoped analytics endpoints: projects list with daily summary, project summary, chart data, service status. 18 service tests.
- **Telegram bot: dashboard button** (#1035): `/dashboard` command generates one-time Redis token (TTL 5min) and sends inline URL button to open the LK dashboard.

## 2026-03-20

### Added
- **Add com.codegen.project_id label to deployed containers** (#1032): Deployer injects `CODEGEN_PROJECT_ID` into the deployed `.env`. service-template `compose.prod.yml` adds `com.codegen.project_id` Docker label to all prod services. Promtail on prod servers (from #1031) discovers containers by this label and extracts `project_id` as a Loki label for per-project log filtering.
- **Promtail on prod servers + expose Loki** (#1031): Expose Loki via Caddy `/loki/*` with Basic Auth for external Promtail push. New Promtail Ansible template scrapes Docker containers by `com.codegen.project_id` label and ships logs to orchestrator over HTTPS. Added Promtail service to monitoring role docker-compose. Pass `orchestrator_hostname` through AnsibleRunner extra-vars. New env vars: `LOKI_PUSH_USER`, `LOKI_PUSH_PASSWORD`, `LOKI_PUSH_PASSWORD_HASH`.

## 2026-03-19

### Added
- **Regression E2E: acceptance criteria on Repository + QA report in admin UI** (task-a775d789): `acceptance_criteria` (Text) and `bot_username` (String) fields on Repository model. Architect agent gets `update_acceptance_criteria` tool — updates repo criteria after story decomposition for regression testing. QA consumer now uses repo `acceptance_criteria` instead of story description — tests full product behavior, not just latest feature. PO agent writes `bot_username` to repo during Telegram token validation. `run_e2e` admin endpoint passes `bot_username` from repo in QAMessage. Admin UI ApplicationDetailPage shows QA run outcome badge + expandable report with failed checks and full markdown. New `GET /applications/{id}/runs` endpoint. Seeded all 4 existing repos with acceptance criteria and bot usernames.

### Added
- **Admin UI: action buttons on entity pages** (#1026): Action buttons across all admin SPA entity pages. New `ConfirmButton` reusable component. TaskDetailPage: Spawn Worker button. New StoryDetailPage with Send to Architect button. New ApplicationDetailPage with Stop/Undeploy/Redeploy/Run E2E buttons + health history table. ProjectDetailPage: secrets editor (masked key-value, add/delete), Create Story form, Deploy from Repo form. New routes `/stories/:id` and `/applications/:id`. New `GET /projects/{id}/config/secrets/keys` API endpoint.
- **Thin API endpoints for admin actions** (#1024): 8 new endpoints on the API service. `POST /stories/{id}/send-to-architect` (validate+transition+publish ArchitectMessage). `POST /tasks/{id}/spawn-worker` (transition+create Run+publish EngineeringMessage). `POST /applications/{id}/stop|undeploy|redeploy` (status transitions + publish DeployMessage). `POST /applications/{id}/run-e2e` (create Run + publish QAMessage). `POST /applications/from-repo` (create Repo+App+port+deploy). `DELETE /projects/{id}/config/secrets/{key}`. Added `ADMIN` to `DeployTrigger`, `DEPLOYING/STOPPING/UNDEPLOYING` to `ApplicationStatus`. API now has `RedisStreamClient` singleton for queue publishing.
- **Queue contracts: Optional story_id + action field** (#1023): `DeployAction` StrEnum replaces `Literal["create", "feature", "fix"]` — adds `stop` and `undeploy` values for lifecycle operations from admin. `QAMessage.story_id` now optional (defaults to `""`) for standalone E2E triggers. New `deploy_lifecycle` module handles stop/undeploy via SSH, skipping the full DevOps subgraph. QA consumer uses `application_id` for inflight dedup when no story. Engineering and architect consumers verified compatible with direct publish.

### Changed
- **Decouple QA consumer from story lifecycle** (#1030): QA consumer stripped of all story transitions (`_transition_story_safe`, `publish_story_event`) and fix task creation. Now only updates `run.status` and `run.result` with a `QAOutcome` enum (PASSED/FAILED/EXHAUSTED/ERROR). Added `RunType.QA` and `QAMessage.run_id` to shared contracts. Dispatcher creates QA run before publishing QAMessage. New `supervise_testing_stories()` in dispatcher polls TESTING stories, reads QA run outcome, and routes: PASSED → complete story, FAILED → create fix task + redispatch to engineering, EXHAUSTED/ERROR → fail story. QA is now a pure technical worker, same pattern as deploy (#1006).
- **Decouple deploy worker from story lifecycle** (#1006): Deploy worker stripped of all story transitions (`_transition_story_safe`, QA handoff, retry tracking, admin notifications). Now only updates `run.status` and `run.result` with a `DeployOutcome` enum (SUCCESS/SMOKE_FAILURE/CODE_FIX/RETRY/GIVE_UP). New `supervise_deploying_stories()` in dispatcher polls DEPLOYING stories, reads deploy run outcome, and routes: SUCCESS → TESTING + QA, CODE_FIX → engineering redispatch, RETRY → redeploy with counter, GIVE_UP → FAILED + admin notification. Added `story_id` FK to Run model for efficient querying. Deploy is now a pure technical worker callable outside story context.

### Added
- **Admin UI: Settings page** (#1025): New `/settings` page in admin SPA with two tabs. System Configs tab shows all configs grouped by category (scheduler, supervisor, deploy, health, llm) with inline edit per row. Agent Configs tab shows expandable cards with prompt textarea editor, model/temperature fields. Sidebar navigation item added.
- **SystemConfig model + API + ConfigStore** (#1020): New `system_configs` DB table for externalizing operational constants. CRUD API at `/api/system-configs/`. `ConfigStore` client in shared/ with TTL cache. Seed script populates 29 defaults from YAML (`make seed`). Scheduler validates all required configs at startup (fail-fast). Replaced hardcoded constants in 12 scheduler/langgraph task modules with DB-backed values.

### Changed
- **Unified worker result API** (task-1b2bdf73): Replaced three HTTP endpoints (`/complete`, `/failed`, `/blocker`) with single `POST /result` accepting `{success: true/false}`. Added `/infra/compose` proxy route so workers use only `localhost:9090`. Captures agent stdout tail (~10KB) for debugging. Auto-resumes Claude agents once if they exit without calling `/result`. Merged `reject_reason`/`block_reason` into `gave_up_reason` across SpawnResult, developer node, and engineering consumer.
- **EngineeringStatus StrEnum** (task-9f294c98): Replaced 6 bare `engineering_status` strings with `EngineeringStatus(StrEnum)` — 4 values: IDLE, DONE, GAVE_UP, FAILED. Merged `_handle_worker_blocked` + `_handle_worker_reject` → `handle_worker_gave_up` (both → WAITING_HUMAN_REVIEW). FAILED is transient: supervisor retries or escalates to GAVE_UP when retries exhausted. Removed `NON_RETRYABLE_REASONS` — semantics encoded in task status. Fixed bug where `block_reason` path returned `"blocked"` indistinguishable from generic crash.

### Fixed
- **Restored Makefile overrides in worker-wrapper** (task-ae3ca2fb): Re-added `_inject_makefile_overrides()` removed in b8864abd. Override targets now `curl localhost:9090/infra/compose` (compose proxy from task-1b2bdf73) instead of deleted `orchestrator` CLI. Fixes `make migrate`, `make dev-start svc=db` inside worker containers.
- **QA consumer resolves wrong application** (task-611d788f): QA consumer called `list_applications({"project_id": ...})` but the API has no `project_id` filter — returned ALL applications, picked wrong one (e.g. codegen_orchestrator instead of weather-bot). Now threads `application_id` from deployer → deploy result → QAMessage → QA consumer, using single `GET /applications/{id}` instead of broken list+filter. Also: fix tasks now created with `status=todo` (was defaulting to `backlog`, so dispatcher never picked them up). Replaced dict soup with `QAServerInfo` dataclass and `ApplicationDTO` for typed API responses.

## 2026-03-18

### Removed
- **orchestrator-cli package** (task-b2401a6e): Deleted `packages/orchestrator-cli/` entirely (~1500 lines). Agent now reports results via curl to localhost:9090 (`/complete`, `/failed`, `/blocker`) and manages infrastructure via curl to worker-manager compose proxy. Updated INSTRUCTIONS.md, Dockerfile, container env vars (removed `ORCHESTRATOR_API_URL`, `ORCHESTRATOR_REDIS_URL`, renamed `ORCHESTRATOR_WORKER_MANAGER_URL` → `WORKER_MANAGER_URL`). Removed `shared/schemas/tool_groups.py` and `tool_registry.py` (CLI doc generation, unused by services). Cleaned up CI matrix, test scripts, and docs.
- **result_parser from worker-wrapper** (task-dc3de88a): Deleted `result_parser.py` and stdout-based result parsing (`<result>` tags, `## REJECTED`, `## BLOCKED` markers). Agent results now flow exclusively through HTTP server (localhost:9090). Simplified `execute_agent()` to subprocess lifecycle only. Removed unused `_get_git_head()` and `_extract_git_commit_sha()`. Watchdog intact: agent exits without HTTP call → auto-fail.

### Added
- **HTTP result server in worker-wrapper** (task-7397ff9b): Added localhost:9090 HTTP server that runs alongside the agent subprocess. Three POST endpoints (`/complete`, `/failed`, `/blocker`) with Pydantic validation — agent gets 400 on bad payload (can retry), 409 on duplicate. HTTP result takes priority over stdout parsing; backward compatibility preserved. Watchdog auto-publishes `failed` if agent exits without reporting. First step of decoupling workers from shared package.

### Fixed
- **Architect 422 on story.start** (hotfix): Architect LLM tool `transition_story` now catches 422 and returns current story state instead of crashing. Fixes race where PO already transitioned story to `in_progress` before architect runs.
- **Deploy failure classifier blind** (hotfix): `wait_for_run_completion` now fetches GH Actions failure logs (`get_workflow_failure_logs`) and includes failed job/step names in the RuntimeError message. The deploy classifier now sees "Job 'deploy' failed: Step 'Deploy via SSH' failed" instead of just "failure".
- **Deploy retry loop after PR merge** (hotfix): `create_pull_request` now searches closed/merged PRs when 422 occurs (was only searching open). `complete_stories` detects merged PRs and triggers deploy directly instead of trying to create a duplicate PR. Breaks the infinite loop: deploy fail → in_progress → create PR (422) → exception → retry.

## 2026-03-17

### Added
- **Shared Pydantic DTOs for API entities** (task-a2a69435, steps 1-3): Added response+request DTOs (`TaskDTO`, `TaskCreate`, `TaskUpdate`, `TaskEventDTO`, `TaskEventCreate`, `StoryDTO`, `StoryCreate`, `StoryUpdate`, `RepositoryDTO`, `RepositoryCreate`, `RepositoryUpdate`, `ApplicationDTO`, `ApplicationCreate`, `ApplicationUpdate`, `IncidentDTO`, `IncidentCreate`, `IncidentUpdate`) to `shared/contracts/dto/`. Moved `IncidentStatus`/`IncidentType` enums from model to DTO (model re-exports for backward compat). 41 new unit tests. Client migration follows in steps 4-9.

### Changed
- **Migrate API clients to Pydantic DTOs** (task-a2a69435, steps 4-9): Migrated all service API clients from raw `dict` returns to typed Pydantic DTOs. Scheduler: 29 methods typed, ~80 caller sites migrated. LangGraph: 17 typed methods, ~80 caller sites migrated (generic `get/post/patch/delete` kept as dict for LLM-facing tools). Scaffolder: `get_project()` → `ProjectDTO`, `get_repository()` → `RepositoryDTO`. Infra-service: `get_server()` → `ServerDTO`. All `["field"]`/`.get("field")` access patterns replaced with attribute access. 250+ unit tests passing across 9 services.
- **Refactor large files (>400 LOC) — extract helpers** (task-d103f639): Extracted helpers from 10 files exceeding 400 LOC. `manager.py` 920→543 (garbage_collector, git_ops, scaffold_phase), `engineering.py` 881→299 (engineering_result_handler, story_context), `deploy.py` 867→383 (deploy_failure_handler, deploy_result_handler, deploy_precheck), `task_dispatcher.py` 738→265 (story_completion, supervisor, pr_poller), `rag.py` 689→269 (rag_ingest, rag_search), `node.py` 642→391 (operations, handlers), `devops/nodes.py` 639→61 (secret_resolver, deployer), `tasks.py` 625→384 (_task_helpers, _task_actions), `po/tools.py` 605→191 (tools_shared, tools_projects, tools_stories), `developer.py` 513→397 (developer_tasks). All original modules re-export via `__all__` for backward compatibility.

### Fixed
- **Contract violations from audit** (hotfix): Replaced hardcoded `"todo"` with `TaskStatus.TODO.value` in webhooks, removed `os.getenv("API_URL", default)` (fail fast), replaced hardcoded queue name strings with `shared/queues.py` constants in projects router, replaced hardcoded status strings with `TaskStatus` enum in engineering consumer, centralized `STORY_WORKERS_KEY` in `shared/queues.py` (was duplicated in langgraph + scheduler).
- **cadvisor parser: cgroup v2 Docker containers filtered out** (hotfix): `_is_real_container` rejected all containers with `id` starting with `/system.slice`, but on cgroup v2 (systemd) Docker containers have `id=/system.slice/docker-<hash>.scope`. Fix: allow `/system.slice` entries that contain `/docker-` in the path. This was causing the Containers tab in the admin UI to show no data despite 22 containers running.

### Added
- **HTTP health prober for deployed applications + SSL expiry check** (task-d378415c): New `app_health_prober.py` — probes each deployed application's `/health` endpoint via HTTP, tracks response times, consecutive failure detection (SERVICE_DOWN incident after 3 fails), SSL cert expiry monitoring (SSL_EXPIRING incident within 7 days), auto-resolves incidents on recovery, computes 24h uptime%. New `ssl_checker.py` — socket-based SSL cert expiry extraction. Added `SSL_EXPIRING` to `IncidentType` enum. Extended `SchedulerAPIClient` with application CRUD methods. Integrated into existing `health_check_worker` loop (runs after server checks). App health history cleanup in daily job. 30 new unit tests + integration tests.
- **Admin UI: application health status and response times** (task-fb032b50): Extended Application model with `response_time_ms`, `ssl_expires_at`, `uptime_pct_24h` fields. New `application_health_history` table (time-series, 7-day retention) with GET/POST/DELETE API endpoints. Enhanced admin applications table with health status dot, response time, uptime %, SSL expiry columns. Expandable application rows with overview cards and response time area chart (1h/24h toggle via Recharts). 19 unit + 4 integration tests.
- **Admin UI: extended server health dashboard** (task-204ef921): Rewrote ServersPage with tabbed expandable rows — Overview (health summary cards: CPU, load avg, network errors, containers, uptime + freshness indicator), Containers (per-container CPU/RAM table from cadvisor metrics), Charts (CPU/RAM/Disk area charts via Recharts with 1h/24h toggle), Incidents (history table with status badges). Added CPU usage bar to main table. New types: MetricsHistoryEntry, ContainerMetrics, Incident. New utils: formatBytes, formatUptime, freshnessColor. First Recharts usage in the project.
- **Health checker worker** (task-47f2fc7c): Implemented `health_check_worker` with HTTP polling of node_exporter (:9100) and cadvisor (:8080) for managed+active servers. Parses Prometheus metrics via existing parser, updates Server health fields (CPU%, load avg, RAM/disk, container counts, uptime), appends metrics history snapshots. Auto-creates `SERVER_UNREACHABLE` incidents on HTTP failure and `RESOURCE_EXHAUSTED` on RAM/disk >90%, with dedup (no duplicates while active incident exists). Auto-resolves unreachable incidents on recovery. Telegram admin notifications on incident creation/resolution. Daily cleanup of metrics history >7 days. New API endpoint: `DELETE /api/servers/metrics-history`. Extended SchedulerAPIClient with incident + metrics history methods. 17 unit tests.
- **Prometheus text format parser** (task-58d52adf): Pure parser module for node_exporter + cadvisor `/metrics` endpoints. Generic `parse_prometheus_text()` handles the full exposition format (labels, timestamps, scientific notation, +Inf, NaN). `extract_node_metrics()` computes CPU% from idle ratio, RAM/disk from `/proc` values (root mount only), load avg, uptime, network errors. `extract_container_metrics()` groups cadvisor data per container, filters system entries. Public API: `parse_node_exporter(text)` and `parse_cadvisor(text)`. 38 unit tests + realistic fixture-based integration tests.
- **Server health metrics model + history table** (task-107966ae): Extended Server model with 9 health metric columns (cpu_usage_pct, load_avg_1m/5m/15m, network_rx_errors/tx_errors, container_count_running/total, uptime_seconds). New `server_metrics_history` table for 7-day retention time-series snapshots (server_handle FK, recorded_at, metrics JSON, composite index). Updated ServerDTO/ServerUpdate/ServerRead schemas. New API endpoints: `GET/POST /{handle}/metrics-history`. PATCH handler accepts health fields + last_health_check. 19 unit + 4 integration tests.
- **Provisioning: node_exporter + cadvisor + UFW rules** (task-a0a40102): Extended monitoring Ansible role with cadvisor container alongside existing node_exporter. UFW rules restrict ports 9100/8080 to orchestrator IP only (`ORCHESTRATOR_PUBLIC_IP` env var). Monitoring role now included in `provision_software.yml` after Docker setup. `AnsibleRunner` passes `orchestrator_ip` as Ansible extra var. Server vps-267180 configured and verified — both `/metrics` endpoints return data. 19 unit tests.

## 2026-03-16

### Fixed
- **Deploy failure classification and worker rejection pipeline** (task-3a06bf14): Fixed broken classifier model ID (`claude-haiku-4-5-20251001` → `claude-haiku-4-5`). Replaced binary CODE/INFRA classification with three-way CODE_FIX/RETRY/GIVE_UP. Changed fallback from CODE to RETRY (safer — retrying wastes less than spawning a useless worker). Added GIVE_UP handler (story→failed, admin notified, worker deleted). Wired up worker rejection pipeline: DeveloperNode now checks `reject_reason` → sets `worker_rejected` status → engineering consumer routes to `_handle_worker_reject()` (was dead code). Added reject-first sanity check as Step 0 in worker INSTRUCTIONS.md.
- **service_deployments `updated_at` missing server default** (hotfix): Original migration `73b707900b42` created the `service_deployments` table with `created_at DEFAULT now()` but `updated_at` without a default, causing `NotNullViolationError` on every INSERT. Deploy-worker's `_create_deployment_record` silently failed (caught exception). Migration `42e0acc86b20` adds the missing `server_default=now()`.

### Removed
- **Prompts tab in admin panel** (hotfix): Removed the "Prompts" tab from worker detail page, `/prompts` and `/prompt-history` API endpoints, Redis persistence of `task_md` and `prompt_history`, and related tests/types. The `-p` argument is now a hardcoded constant — no value in tracking it.

### Changed
- **Deploy → QA handoff** (hotfix): Deploy consumer now transitions story to `TESTING` and publishes `QAMessage` to `qa:queue` instead of completing story directly. Worker container not deleted (QA may need it for fixes). Standalone webhook deploys (no story_id) bypass QA.

### Added
- **Ansible role: qa_runner provisioning** (task-b6e972e4): New Ansible role that provisions prod servers for QA. Installs Claude Code CLI (standalone binary, no Node.js), telethon+httpx in venv. Creates 2GB swap (prevents OOM on 4GB servers). Auth via `.credentials.json` copy (same session pattern as worker-manager). Included in `site.yml` and `provision_software.yml`. Tested on prod — Claude Code responds. 13 unit tests.
- **QA consumer skeleton** (task-22130356): Post-deploy QA consumer that reads from `qa:queue`, SSHes to prod server, runs Claude Code with story-based QA prompt, and parses the JSON result. Pass → story completed + user notified. Fail → fix task created + story rolled back to `in_progress`. Inflight dedup (25 min TTL), max 2 QA→Engineering loops. `qa-worker` service in docker-compose. 24 unit tests.
- **TESTING story status + QA queue contract** (task-4dbe7a76): Foundation for post-release QA. New `StoryStatus.TESTING` enum value with transitions `DEPLOYING → TESTING → {COMPLETED, IN_PROGRESS, FAILED}`. New `POST /api/stories/{id}/test` endpoint. `QAMessage` contract in `shared/contracts/queues/qa.py`. `QA_QUEUE` + `QA_GROUP` constants and topology binding. 15 new tests.
- **PR merge polling** (hotfix): Dispatcher now polls GitHub for merged PRs on stories in `pr_review` status every 30s. Eliminates dependency on GitHub webhook for the `pr_review → deploying` transition. New `list_pull_requests()` method on `GitHubAppClient`.
- **Deploy failure LLM classifier** (hotfix): Deploy worker now classifies failures as CODE vs INFRA using haiku before dispatching to engineering. INFRA failures (timeouts, network, resource limits) retry deploy instead of wasting an engineering worker. After max retries, story is marked failed for HITL. Extracted `_track_deploy_retry()` helper from `_handle_deploy_failure()`.

## 2026-03-15

### Added
- **Branch protection after scaffold** (task-709e1861): After scaffolder creates a repo and pushes initial commit, GitHub branch protection rules are now set on `main` — requires PR for merge, requires `ci` status check to pass. Non-fatal: scaffold succeeds even if protection setup fails. New `update_branch_protection()` method on `GitHubAppClient`.

### Added
- **Feature branches for stories** (#1011): Workers now operate on story-level feature branches (`story/{story_id}`). Branch name flows through the full pipeline: engineering consumer → developer node → worker spawner → task dispatcher → worker manager → worker wrapper. Worker manager creates/checkouts the branch in containers. Worker wrapper reports branch in result dict and pulls from current branch instead of hardcoded `main`. INSTRUCTIONS.md updated to encourage pushing on feature branches.
- **PR-based CI gate** (#1014): Replaced polling-based CI gate (`_ci_gate.py`, 531 lines deleted) with a PR-based flow. When all story tasks complete, task dispatcher creates a PR from `story/{id}` → `main` and enables auto-merge. CI runs on the PR; green CI → auto-merge → webhook → deploy. Red CI on story branch → webhook creates fix task and transitions story back to `in_progress`. New `PR_REVIEW` story status. Added 4 GitHub client methods (`create_pull_request`, `enable_auto_merge`, `merge_pull_request`, `close_pull_request`). Webhook handler extended to handle `pull_request` (merged) and `workflow_run` (CI failure on story branches) events.

### Changed
- **TASK.md moved to /workspace/**: TASK.md now lives in the workspace directory (`/workspace/TASK.md`) instead of `/home/worker/TASK.md`. Worker-manager injects it there on create; wrapper updates it each turn. After task completes, wrapper archives TASK.md + REPORT.md into `.story/old_tasks/{task_id}.md` — next worker sees full history. `.story/` is auto-gitignored.
- **Minimal `-p` prompt for Claude workers**: Wrapper now passes a one-line redirect ("Read TASK.md") as `-p` instead of the full task content. Full task stays in TASK.md file — Claude reads it on demand, keeping context window clean. Removed self-referential TASK.md references from developer.py and INSTRUCTIONS.md.
- **Merge AUDIT_REPORT.md into REPORT.md**: Removed separate AUDIT_REPORT.md concept from e2e-run skill. Workers already write REPORT.md with Issues+Suggestions sections (per INSTRUCTIONS.md) — that IS the audit report. Worker reports collected via task events API.
- **Filter scaffolder tree output**: `_capture_tree()` now excludes `.venv`, `node_modules`, `.git`, `__pycache__`, `.mypy_cache`, `.ruff_cache` from the tree passed to the architect. Same exclusion set as the admin panel workspace browser. Saves tokens in architect context.
- **E2E skill: save reports before cleanup**: Step 7 now explicitly saves worker reports to local files before Step 9 DB cleanup. Previously reports could be lost when task_events were deleted.

### Added
- **Task archiving (`.story/old_tasks/`)**: After each task, wrapper merges TASK.md + REPORT.md into `.story/old_tasks/{task_id}.md`. Next worker can browse previous tasks for context without force-fed story_context in the prompt.
- **Hybrid --resume session management**: `SessionManager.clear_session()` method + `clear_session` flag in task messages. `send_task_to_worker()` accepts `clear_session=True` to force fresh Claude CLI session on retries (avoids inheriting errors from failed previous attempt). First task in story: fresh (new worker). Subsequent: `--resume` via stored session.

## 2026-03-14

### Changed
- **Bind PortAllocation to Application** (#task-199b1bcb): PortAllocation now belongs to Application (via `application_id` FK) instead of Project. Application no longer has a single `port` field — ports come from `port_allocations` relationship (one-to-many). `ApplicationRead` API response includes `ports: list[PortAllocationRead]`. Application is created at allocation time (before deploy). Deploy flow simplified — uses state data instead of re-querying allocations.

### Added
- **Application entity + Deployment refactor** (#task-f01a41fe): Introduced `Application` as a first-class runtime entity (repo + server + status), separated from `Deployment` (immutable deploy log). New `ApplicationStatus` enum (not_deployed, running, stopped, down, degraded) and `DeploymentResult` enum (pending, success, failed, canceled). Application CRUD API at `/api/applications/`, server applications endpoint at `/servers/{handle}/applications`. DeployerNode now creates Application records on deploy. Data migration backfills Applications from existing deployments. Admin Servers page shows Applications instead of raw deployment records. 24 new unit tests.
- **Tasks page multi-select filters + sortable columns**: Status and type filters now support multi-select (checkboxes). Status, Priority, Updated column headers are clickable for asc/desc sorting. New `MultiSelect` UI component.

### Changed
- **Unified workspace management around repo_id** (#task-7147c381): All workspace addressing now uses `repo_id` instead of `project_id`. Scaffolder is sole source of truth for workspaces at `/data/workspaces/{repo_id}/`. Removed legacy `WORKSPACE_BASE_PATH` config and `/tmp/codegen/workspaces` volume from worker-manager. Workers now require `repo_id` (RuntimeError if missing). `repo_id` stored in Redis `worker:meta` hash and exposed on introspect API. Workspace browser endpoints use `repo_id`. Frontend resolves `repo_id` via repositories API. Removed dead in-container scaffold phase code. Fixes workspace browser not showing files for projects like lesswrong-random-bot.

## 2026-03-13

### Added
- **Ensure-workspace gate** (#task-0bca0e67): Scaffolding now always runs as a gate before pipeline proceeds. `ScaffoldMessage` gains `mode` field (`full`/`ensure`). New `run_ensure_workspace()` — skips if workspace exists, clones+setups if repo exists on GitHub, errors otherwise. `scaffold_trigger` handles ACTIVE projects with TODO tasks (mode=ensure). `task_dispatcher` checks `workspace_ready` flag before dispatching. Worker-manager GC calls new `POST /repositories/{repo_id}/notify-workspace-deleted` API endpoint to clear `workspace_ready` on deletion. Integration tests in infra suite. Fixes crash when workspace is GC'd and pipeline tries to proceed without it.
- **Workspace browser** (#task-a8f3703f): Workspace as first-class entity keyed by project_id. New `/api/introspect/workspaces/{project_id}/tree` and `/files/{path}` endpoints in worker-manager. Shared `FileTree`/`FileViewer`/`WorkspaceBrowser` React components extracted from WorkerDetailPage. ProjectDetailPage gains "Workspace" tab for browsing project files. Worker Files tab delegates to project workspace when available, falls back to Redis meta for ephemeral workers. 12 new unit tests.
- **Admin SPA: LLM Tracing + Users pages** (#task-df069084): New `/tracing` page with Langfuse iframe. New `/users` page (list) and `/users/:id` detail page with projects tab and tracing tab. Sidebar gains "Users" and enabled "LLM Tracing" items. Project detail page shows Owner link and LLM Tracing section. API `GET /projects/` supports `owner_id` query param filter. Nginx strips `X-Frame-Options`/`Content-Security-Policy` from Langfuse proxy to enable iframe embedding.
- **LangChain → Langfuse tracing integration** (#task-300f55e6): Drop-in LLM tracing via `langfuse` v4 SDK. New `src/tracing.py` utility returns LangChain `CallbackHandler` when `LANGFUSE_PUBLIC_KEY` + `SECRET_KEY` env vars are set (empty = disabled). Wired into all 4 consumers (PO, architect, engineering, deploy) via `config={"callbacks": ...}`. Zero changes to agent/graph code. Env vars added to `.env.example`, picked up by all services via `env_file`.
- **Langfuse v3 infra** (#task-a51fb1cf): Self-hosted LLM tracing stack. Docker-compose adds 4 new services: `langfuse-web` (UI on port 3002), `langfuse-worker` (background processor), `clickhouse` (trace analytics), `minio` (S3-compatible event/media storage). Separate `langfuse` PostgreSQL database via init script. Shared Redis (no auth). Nginx proxy at `/langfuse/` through admin-frontend. `make init-langfuse-db` for existing deployments. Env vars for ClickHouse, MinIO, and Langfuse secrets in `.env.example`.

### Fixed
- **Admin tab state lost on refresh**: Detail pages (Project, Queue, User, Worker) now persist active tab in URL search params. WorkspaceBrowser tree auto-refreshes every 15s. User messages trace polling set to 7s.
- **Audit cleanup**: Use enums (`WorkerStatus.STARTING`, `RunStatus.FAILED/RUNNING`), proper exception chaining (B904), `HTTPStatus.BAD_REQUEST` in telegram handlers, fail-fast on missing `API_BASE_URL` in infra-service.
- **Worker lifecycle cleanup**: `delete_worker()` now cleans `worker:{id}:input/output` streams (were orphaned forever). Orphan GC does reverse check (Redis → Docker) — cleans stale `worker:status` entries where container is gone. Deploy consumer deletes worker container on story complete/fail and calls `clear_story_worker` (was dead code). Workspace GC scans both `WORKSPACE_BASE_PATH` and `SCAFFOLDED_WORKSPACE_PATH`, max_age raised to 35h, cleans stale `workspace:active_projects` entries. Introspect API shows GONE status for stale workers.
- **Architect story spam**: Architect consumer now transitions story to `IN_PROGRESS` immediately on pickup, preventing supervisor from re-publishing the same story every 30s. Also skips stories already decomposed (IN_PROGRESS + has tasks). Supervisor retry counter moved from in-memory dict to Redis (`story:architect_retries:{id}` with 1h TTL) — survives scheduler restarts.

### Added
- **Queue message browser**: New `/debug/queues/{stream}/messages` and `/{stream}/{group}/pending` API endpoints. Queue cards in admin are now clickable → detail page with Messages tab (XRANGE, parsed data preview, expandable JSON, delete with confirmation) and Pending tab (consumer, idle time, delivery count, ack button). Also: `POST ack`, `DELETE message` endpoints.
- **WorkerStatus enum** (`shared/contracts/dto/worker.py`): New `StrEnum` with RUNNING, PAUSED, DEAD, FAILED, STOPPED, GONE, UNKNOWN. Replaced all hardcoded status strings across worker-manager (manager, events, introspect router) and langgraph (worker_spawner). Updated all tests.
- **Admin Phase 2: worker inspector + queues + action buttons** (#task-6d8257e5): Workers list page with live auto-refresh (5s), status badges, project links. Worker detail page with tabbed view: Console (live container logs with tail selector), Prompts (CLAUDE.md + TASK.md viewer), Files (collapsible directory tree + file content viewer with size display). Kill worker button with confirmation dialog. QueuesPage upgraded with proper `DebugQueuesResponse` types (bindings array, status badge, issues warning banner). Task detail page gets Retry button (failed → backlog) and Resume button (WHR → in_dev with guidance textarea). API client extended with `rawDelete`/`rawPost` methods. Full TypeScript types for worker-manager introspection API.
- **Worker-manager introspection API** (#task-716e9208): New `/api/introspect/` router in worker-manager with 7 endpoints — list workers, worker detail (with container info from Docker), container logs (tail param, max 5000), workspace file tree, file content (with path traversal protection via symlink-safe resolve), prompts (CLAUDE.md + TASK.md), and kill worker. Admin-frontend nginx proxies `/wm-api/` → worker-manager. 21 unit tests.
- **Admin auth + single entry point** (#task-d87d08bf): Nginx basic auth on admin-frontend (htpasswd generated from `ADMIN_USER`/`ADMIN_PASSWORD` env vars at container startup). Grafana proxied through `/grafana/` sub-path (no external port). Logs page embeds Grafana dashboard in iframe instead of opening new tab. Closed external ports for Grafana (3000) and API (8000) — only port 3001 exposed. `/health` excluded from auth for Docker healthcheck.
- **Admin frontend scaffold** (#task-57cc3462): React 19 + TypeScript + Vite + Tailwind CSS admin SPA in `services/admin-frontend/`. Sidebar layout with Dashboard, Projects, Tasks, Workers, Queues, Servers pages. Dashboard with live data (project count, tasks by status, queue health with 30s polling). Projects/Tasks list with filters + detail pages with event timeline. nginx multi-stage Docker build on port 3001, proxies `/api/*` → api:8000 (no CORS). Grafana iframe embedding enabled (`GF_SECURITY_ALLOW_EMBEDDING`).
- **Observability stack: Loki + Grafana + Promtail + correlation ID propagation** (#task-52743877): Added `bind_message_context()`/`unbind_message_context()` to structlog correlation module — auto-binds `correlation_id`, `task_id`, `story_id`, `project_id` from Redis stream messages. Applied to all 4 consumer patterns (base worker, PO, scaffolder, worker-manager). All 5 API clients propagate `X-Correlation-ID` header on outbound requests. Docker Compose gains Loki (log aggregation, 7-day retention), Promtail (Docker log scraper), Grafana (pre-provisioned datasource + service-logs dashboard with service/level/correlation_id filters). All services get `LOG_FORMAT` and `SERVICE_NAME` env vars. 9 new unit tests.
- **Architect specs context**: Scaffolder now parses YAML spec files (models, events, domain operations) from generated projects and saves a compact `specs_summary` to `project.config`. Architect agent sees model names, domain operations, and events when decomposing stories. New `spec_extractor.py` module in scaffolder with full test coverage.
- **Architect scaffold wait**: Architect consumer now polls `project.status` before decomposing stories. For new projects, waits up to 5 min for scaffold completion (DRAFT → ACTIVE) instead of running blind without tree/specs context.
- **Parameterized `get_project_spec` tool**: Architect can request detail levels — compact summary (default: model/event/domain names only) or full definitions (`detail="models"`, `"events"`, `"domains"`). Saves tokens by default, deep-dives only when needed.
- **PO `get_story` enriched with runs**: `get_story` tool now fetches runs for each task (id, status, type, error, timing). PO can answer "how's it going?" without needing `get_run_status` for basic info.
- **PO `story_blocked` event**: PO consumer now accepts `story_blocked` system event (previously dropped). PO prompt updated with calm messaging — "specialist is reviewing, work will resume automatically".
- **Runs API `task_id` filter**: `GET /api/runs/` now accepts `task_id` query parameter. `RunRead` schema includes `task_id` field.

### Changed
- **Architect prompt rewrite**: Removed scaffold-centric framing. Focus on "existing service with specs" rather than "scaffolded from template". Added task decomposition philosophy: slice into logical iterations, focus on boundaries between tasks, leave developer freedom for implementation decisions.
- **Developer blocker guidance**: INSTRUCTIONS.md "When You're Stuck" section rewritten. Emphasis on trying to solve problems first, but never shipping code that compromises product quality. "Better to ship nothing than ship something that works incorrectly."

## 2026-03-12

### Added
- **HITL MVP: WAITING_HUMAN_REVIEW + report-blocker + admin resume** (#task-477f5736): Developer agents can now escalate blockers instead of silently shipping workarounds. New `WAITING_HUMAN_REVIEW` status in TaskStatus and StoryStatus with full transition support. `## BLOCKED` marker in worker-wrapper (parallel to `## REJECTED`). `orch report-blocker` CLI command writes blocker reason to stdout. Engineering consumer `_handle_worker_blocked()` transitions task+story to WHR, notifies admin (Telegram, warning level), notifies user via PO (story_blocked event). `POST /tasks/{id}/resume` endpoint for admin to provide guidance and resume (WHR → IN_DEV). Task dispatcher skips WHR tasks and treats `developer_blocked` as non-retryable. Developer prompt updated with "When You're Stuck" section. ~27 new unit tests.
- **Story/Task reopen flow with user_report** (#task-ce845712): PO can now reopen completed stories instead of creating new ones, carrying a `user_report` field that describes what's wrong. New `reopen_story` PO tool calls `/api/stories/{id}/reopen` endpoint and publishes `ArchitectMessage` with `is_reopen=True` + `user_report`. Architect receives reopen context and reviews previous tasks before creating new ones. Developer sees user_report in story context (TASK.md). PO prompt updated to check `list_stories` before `create_story`. New Story model field + Alembic migration. ~20 new unit tests.

### Changed
- **ProjectStatus split: lifecycle + service_status** (#cc4d1a65): Split 13-value `ProjectStatus` enum into 3 focused enums: `ProjectStatus` (lifecycle: draft/active/paused/archived), `ServiceStatus` (runtime: not_deployed/running/degraded/down/stopped), `RepositoryStatus` (active/missing). Engineering/deploy consumers no longer touch `project.status` — only `service_status`. Alembic data migration maps all old values. All status references use enum values, no hardcoded strings. 12+ new unit tests.

### Added
- **PO bot token validation** (`validate_telegram_token` tool): PO now validates Telegram bot tokens via `getMe` API immediately after receiving them. Extracts bot username and stores both `TELEGRAM_BOT_TOKEN` and `TELEGRAM_BOT_USERNAME` as project secrets. Invalid tokens fail fast at PO stage instead of wasting 30+ min on engineering + CI + deploy. PO prompt updated to use new tool instead of raw `set_project_secret` for bot tokens. 5 new unit tests.
- **Container crash logs in smoke failure output**: When smoke test fails, `SmokeTesterNode` SSHes into the deploy server and captures `docker compose logs --tail=50`. Logs are appended to the check `detail` field and flow through the existing deploy→engineering feedback loop, so the fix task receives actual tracebacks (e.g. `ModuleNotFoundError`) instead of bare "HTTP 500". Graceful fallback if SSH fails or `server_handle` is missing. 4 new unit tests.

### Fixed
- **Stale worker auto-cleanup**: `_check_project_lock()` now verifies `worker:status` — workers in terminal states (DEAD/FAILED/STOPPED) get their Redis keys cleaned up automatically, unblocking new task dispatch without manual Redis cleanup. 5 new unit tests.
- **Deploy retry limit (max 3)**: `_handle_deploy_failure()` tracks consecutive deploy attempts per story in Redis. After 3 failures, story transitions to `failed` instead of looping back to `in_progress` — prevents the infinite deploy→fail→redispatch loop that caused hundreds of failed runs and proactive message spam. 4 new unit tests.
- **Deploy deduplication: Redis lock replaces DB race** — replaced non-atomic DB-based `_check_duplicate_deploy` with atomic `SET NX` Redis lock per project. Eliminates the race window where two consumers could both pass the DB check and trigger duplicate `deploy.yml` GitHub Actions runs on the same commit. Lock held for duration of deploy, released in `finally` block. 5 new unit tests.

## 2026-03-11

### Added
- **Deploy→engineering feedback loop**: When deploy succeeds but smoke test fails, or workflow fails entirely, re-dispatch a fix task to `engineering:queue` so the developer agent can fix the code bug. Capped at 2 retry attempts via `deploy_fix_attempt` counter on both `DeployMessage` and `EngineeringMessage` contracts. 7 new unit tests.
- **PO proactive secret collection**: PO now identifies required paid API keys (OpenRouter, Stripe, etc.) from the project description and asks the user before starting engineering work.

### Fixed
- **Proactive message spam filter**: Deploy failures, smoke failures, precheck errors, and "all tasks done" messages no longer reach the user via `po:proactive`. Only two events are sent: (1) deploy success, (2) permanent story failure (user-friendly message, no technical details). Eliminates the 11+ technical spam messages seen in e2e runs.

- **Deploy auto-fallback create→feature when dir exists**: When `action=create` precheck fails with "dir already exists" (stale project.status after initial deploy), auto-switch to `action=feature` instead of failing. Eliminates the most common manual intervention from e2e runs. 4 new unit tests.

- **CI-check task fails on "no commit made"**: CI-check tasks (created_by=system) that find nothing to fix would fail with "Worker reported success but no commit was made", retry 3 times, then fail the entire story. Added `allow_no_commit` flag to `EngineeringState` — set for CI-check tasks via `_is_ci_check_task()`. Developer node returns `done` instead of `blocked` when worker succeeds without commit. Engineering consumer skips commit gate and CI gate, marks task done directly. E2E validated on fortune-teller-bot: "All 36 tests pass, CI green" → task done (previously: 3 retries → story failed). 5 new unit tests.

### Added
- **Story `deploying` status — deploy gate before completion**: Story no longer transitions to `completed` until deploy succeeds. New `DEPLOYING` status in StoryStatus enum with transitions: IN_PROGRESS → DEPLOYING → COMPLETED (on success) / IN_PROGRESS (on failure). Scheduler's `complete_stories` now transitions to `deploying` + triggers deploy with correct `action` (`feature` for already-deployed projects, `create` for new). Deploy worker completes story on success, rolls back to `in_progress` on any failure. Added `story_id` to `DeployMessage` contract, `POST /stories/{id}/deploy` endpoint, `_handle_deploy_failure` helper. 4 new transition tests.

### Fixed
- **Deploy action always `create` for already-deployed projects**: `complete_stories` now checks `project.status == ACTIVE` to send `action=feature` instead of `create`, preventing pre-check failures on update deploys.
- **Contract violations: hardcoded status strings → shared enums**: Replaced ~30 hardcoded status string literals (`"todo"`, `"done"`, `"failed"`, `"in_dev"`, `"scaffolding"`, etc.) with `TaskStatus`, `StoryStatus`, `ProjectStatus` enums from `shared/contracts/dto/` across 7 files in 4 services (scheduler, langgraph, scaffolder, api). Prevents silent breakage if enum values are renamed.
- **Contract violations: hardcoded Redis queue names → shared constants**: Removed 4 locally-defined queue name constants (`PROVISIONER_QUEUE`, `COMMAND_STREAM`, `RESPONSE_STREAM`, `WORKER_COMMANDS_STREAM`) that duplicated `shared/queues.py`. Added `WORKER_RESPONSES` constant. Replaced 5 direct `redis.xadd()` calls with `RedisStreamClient.publish_message()`/`publish()` where the abstraction was available. Updated 5 test files.

### Changed
- **CI gate: one push per story instead of per task** (#1004): CI no longer runs after every engineering task — only once at story end via the CI check task. Ordinary story tasks commit but don't push; CI check task (created_by=system) pushes and runs CI gate. Saves GitHub Actions minutes proportional to task count. Fixed `append_ci_check_task` creating CI task without `status: "todo"` (stuck in backlog forever). Extracted `_should_run_ci_gate()` and `_run_ci_gate_and_handle_failure()` helpers. Updated worker prompt to "Do NOT push unless task explicitly tells you to". 9 new tests.

## 2026-03-10

### Added
- **Live pipeline test suite** (3-tier E2E): Structured test suite split by pipeline phases — scaffold (~30s), engineering (~3.5min), full deploy (~7-10min). Module-scoped async fixtures share one pipeline run across multiple tests. Shared `pipeline_helpers.py` with all phase helpers, cleanup, and debug dump. Makefile targets: `test-live-smoke`, `test-live-engineering`, `test-live-mega`, `test-live-pipeline` (all). Auto-cleanup always runs (GitHub repos, server containers, DB records via SQL cascade, port allocations). Debug dump captures ctx + last 30 lines of docker logs on failure. Queue flush at fixture start prevents stale message pollution. 9/9 tests passing.
- **Smart CI failure triage: worker reject signal** (#task-61339aef): Workers can now signal `## REJECTED` when a CI failure is infrastructure-related (missing secrets, registry auth, Docker issues). ResultParser detects the marker, SpawnResult carries `reject_reason`, CI gate stops retries immediately. Engineering consumer transitions task to `failed` with `failure_metadata.failure_reason=worker_rejected`, story to `failed` with reject metadata, and calls `notify_admins()`. Dispatcher skips siblings of rejected tasks; supervisor skips rejected tasks from retry. CI-fix prompt template includes structured reject instructions. 27 new tests across 6 test files.

### Fixed
- **ProjectStatus enum missing "error"**: DevOps DeployerNode writes `"error"` string literal but `ProjectStatus` enum lacked `ERROR = "error"`. Scheduler's `get_projects()` → Pydantic ValidationError → crash loop → dispatcher never runs → tasks stuck at "todo". Added `ERROR = "error"` to enum.
- **Scaffolder: create GitHub repo before clone** (E2E pipeline blocker): Scaffolder tried to `git clone` a repo that didn't exist on GitHub. Added `create_repo()` call before clone (idempotent, ignores 422).
- **Scaffolder: update `git_url` after repo creation** (E2E pipeline blocker): Repository `git_url` stayed as `pending://` placeholder — CI gate couldn't find the repo. Scaffolder now updates `git_url` to real GitHub URL after creating the repo.
- **github_sync UUID serialization**: `_ingest_to_rag` passed UUID object to `json.dumps`, causing `TypeError`. Fixed with `str(project.id)`.
- **TaskCreate schema: missing `status` field** (E2E pipeline blocker): `TaskCreate` Pydantic schema didn't include `status` — Pydantic silently dropped it, SQLAlchemy used `default=backlog`. Router also hardcoded `TaskStatus.BACKLOG` in both `create_task` and `push_task`. Now accepts `status` from request body (default: backlog). Architect tasks correctly created as `todo`.
- **PO: missing Repository creation** (E2E pipeline blocker): `create_project` PO tool created Project + Story but no Repository. `scaffold_trigger` requires repository to exist (`get_repositories()` check). Added `POST /api/repositories/` call with placeholder `git_url` to `create_project` tool.
- **Scaffolder container not running** (E2E pipeline blocker): `scaffolder` service defined in docker-compose.yml but never built/started. Built and started with `docker compose up -d --build scaffolder`.

### Changed
- **Architect prompt: prefer fewer tasks**: Rewrote task creation rules to prefer fewer, larger tasks. One task per story is fine for simple projects. Only split when genuinely different concerns.
- **Makefile: `stop` is now alias for `down`**: Removed duplicated logic; `down` kills worker containers and cleans network.
- **docker-compose: scaffolder gets `GITHUB_ORG`**: Scaffolder now receives `GITHUB_ORG` env var; fixed PEM mount path typo.

### Added
- **E2E Pipeline V2 smoke test** ([report](e2e_results/pipeline_v2-20260310.md)): First full-flow test of Pipeline V2 (PO → Scaffolder → Architect → Dispatcher → Worker). Confirmed PO→Architect flow works end-to-end. Found 3 blocking bugs (all fixed), 1 medium (self-resolving after fixes). Architect decomposed "string reverser bot" into 4 chained tasks in ~42s.

## 2026-03-09

### Added
- **Scaffolder microservice**: New `services/scaffolder` service that consumes from `scaffold:queue` and prepares project repositories (copier + make setup + git push). Runs before architect so it can see the project tree. Added `ScaffoldMessage` contract, `SCAFFOLD_QUEUE`/`SCAFFOLD_GROUP` queue constants, scheduler trigger that detects draft projects with stories. Docker-compose entry with workspace + service-template volume mounts. 22 new unit tests across scaffolder, scheduler, shared, and API services.
- **Worker reuse per story** (#1002): Spawn worker container once per story, reuse for subsequent tasks (~50s saved per task). Redis hash `story:workers` maps story_id→worker_id. Engineering consumer looks up existing worker via `get_story_worker()`, passes to DeveloperNode which uses `send_task_to_worker()` (with fallback to `request_spawn()` on timeout). Scheduler cleans up worker on story complete/failure via `DeleteWorkerCommand`. Added `story_id` field to `EngineeringMessage`. Langgraph service tests added to CI matrix. 39 unit tests + 6 service tests.
- **Pipeline failure supervisor** (#1001): Three supervisor functions in the 30s dispatch loop: `supervise_stuck_stories` retries architect for stories stuck in `created` >5min (up to 3 retries); `supervise_failed_tasks` reopens failed tasks (up to `max_iterations`) or fails story with sibling cancellation; `supervise_stuck_tasks` times out `in_dev` tasks after 30min. Terminal failures notify user via PO. Added `StoryStatus.FAILED` with `/stories/{id}/fail` endpoint, `current_iteration` to `TaskUpdate` schema. 16 new tests.
- **PO tools contract tests**: 15 unit-level contract tests that import API Pydantic schemas directly and validate PO tool payloads (ProjectCreate, StoryCreate, MergeSecretsRequest). 9 integration tests that call PO tools against a real API with DB, validating full roundtrip (PO tool → HTTP → API → DB → response). New `po-tools` suite in CI integration tests matrix.

### Changed
- **Worker-manager mounts workspace by repo_id + story context** (#18): Worker-manager now mounts pre-scaffolded workspaces by `repo_id` instead of running copier+setup inside containers. Developer node passes `repo_id` instead of `ScaffoldConfig` to worker spawner. Engineering consumer builds story context (previous tasks + events) and passes it to worker via task message, giving full continuity so workers don't re-gather info each time. Extracted `_resolve_allocations()` helper. Added `get_task_events()` to langgraph API client, `story_context` field to `EngineeringState`. 11 new tests.
- **Architect: scaffolded-aware decomposition** (task-2378004c): Rewrote architect system prompt to understand scaffolded project state — creates tasks only for business logic diff, not infrastructure. Enhanced `get_project_spec` tool to surface `tree` from config and strip noisy fields (secrets, env_hints). Auto-appends CI check task after architect LLM finishes creating tasks. 15 new tests.
- **Update Ruff to 0.15.5**: Bumped ruff from 0.8.4 to 0.15.5 in pyproject.toml and CI. Reformatted 17 test files (parenthesized assertion style). No functional changes.
- **Remove Docker tooling, use `uv run` everywhere**: Deleted `tooling/Dockerfile`, `docker-compose.tools.yml`, `.pre-commit-config.yaml`. Rewrote `make lint`/`format`/`lock-deps` to use `uv run` directly. Git hooks now require `uv` instead of Docker. CI uses `uv sync` + lockfile ruff instead of `--with ruff==VERSION`. Single source of truth for ruff version: `pyproject.toml` + `uv.lock`.

### Refactored
- **Architect: migrate to LangGraph ReAct agent** (#36): Moved architect from scheduler plain function to langgraph service as a ReAct agent with tool use. New `architect` Docker service (same langgraph image, separate entrypoint). Created 5 architect tools (get_story, get_project_spec, get_tasks_by_story, create_task, transition_story). Added `group` parameter to base worker for custom consumer groups. Removed architect code from scheduler service. 22 new unit tests.
- **LangGraph service directory refactoring** (#35): Renamed `src/workers/` → `src/consumers/` (with `_worker` suffix dropped from files), dissolved `src/worker/` module into `src/` root, centralized PO prompts under `src/prompts/po/`. Updated Dockerfile, docker-compose, integration test config, and all imports. Pure structure change — no business logic modifications.

### Added
- **Architect node — story decomposition into tasks + task dispatcher** (#34): Full architect pipeline: story → architect:queue → LLM decomposition → N tasks with `blocked_by_task_id` chains → task dispatcher → engineering runs. Architect consumer runs in scheduler with concurrent processing (Semaphore(5)). Task dispatcher polls every 30s: dispatches unblocked todo tasks with cumulative context from sibling task events, completes stories when all tasks done (triggers deploy + PO notification). Engineering worker now updates task status alongside run status and skips per-task deploy. PO `create_story` tool publishes to architect:queue instead of engineering:queue.

## 2026-03-08

### Fixed
- **Deploy: inter-service URL uses docker service name** (#54): `BACKEND_API_URL` and similar inter-service variables now resolve to `http://backend:8000` (docker DNS) instead of `http://<external_ip>:<port>`. Added `API_URL` to `COMPUTED_EXACT` in env_analyzer. External-facing URLs (`deployed_url`, `DEPLOY_HOST`) remain unchanged.

### Refactored
- **Split engineering_worker.py** (#18): Extracted CI gate logic into `_ci_gate.py` (480 LOC) and repo setup into `_repo_setup.py` (124 LOC). Main file reduced from 1114 to 545 LOC. Pure internal refactoring — no behavior changes.

### Changed
- **Decouple shared/ from Docker builds** (task-7e9aed9c): Replaced `pip install shared` with plain `COPY shared + PYTHONPATH=/app` in all 6 service Dockerfiles and worker-base-common. Moved shared's pip deps into each service's own pyproject.toml. Narrowed `WORKER_SOURCE_HASH` to only hash worker-relevant shared submodules. Fixed `.dockerignore` to exclude nested `.venv/` dirs. Rebuild after shared/ change: ~10s (was ~5min).

### Added
- **Deploy Pre-Check** (#21): Added `action` field (create/feature/fix) to `DeployMessage` contract. Engineering worker propagates action to deploy message on auto-deploy. Webhook-triggered deploys default to action=feature. Deploy worker SSH-checks `/opt/services/<name>/` before deploying: create fails if dir exists (leftover cleanup needed), feature/fix fails if dir absent (never deployed). Added `asyncssh` dependency.

### Changed
- **Dockerfile layer caching optimization** (#21 deviation): Split shared package install into deps-first + code-only steps across all service Dockerfiles for better layer caching. Multi-stage Claude CLI install in worker-base-claude avoids re-downloading on base image changes.

### Fixed
- **compose.dev.yml ports conflict with worker containers** (task-f9aadfc1): Compose runner now injects `.codegen-ports.yml` override that clears published ports (5432, 6379) for worker projects. Workers communicate via Docker DNS on isolated networks, so published ports are unnecessary and conflicted with orchestrator's own postgres/redis.

### Added
- **Seed DB — stories, repositories, historical tasks** (task-f7cd9611):
  - Updated project status to `developing`, migrated repo URLs to `project-factory-organization`
  - Created repositories for hammurabi-game-bot and todo-api with correct `provider_repo_id`
  - Created 2 new stories: "Refactoring & code health", "Dev process automation"
  - Created technical story "Rust migration" (for future Story type field: product/technical)
  - Linked all 40+ orchestrator tasks to stories
  - Imported 11 done + 12 backlog + 5 Rust tasks from service-template backlog
  - Cleaned up 8 smoke/test tasks and 3 test stories
  - Created task for "Replace Milestone with Story type field"
  - Updated `/triage` skill: story matching on task creation, template tasks via API with repository_id

### Changed
- **Replace Milestone with Story type field** (task-6fe23f2a): Added `type` field (product/technical) to Story model, schemas, and router with filter support. Dropped `milestones` table, `milestone_id` from tasks, and all Milestone code (model, schemas, router, DTO, tests, seed script). Updated product-planning documentation and related docs. Set Rust migration story to type=technical.
- **Project ID → UUID + schema cleanup** (task-7163e7ac): Changed `Project.id` from `String(255)` to native PostgreSQL `UUID` with auto-generation. Migrated all 13 FK `project_id` columns to `Uuid` type. Removed legacy `github_repo_id` and `repository_url` from Project model. Added `visibility` column to Repository. Migrated webhook lookup to `Repository.provider_repo_id`. Added `get_primary_repository` to API clients. Updated all DTOs, schemas, routers, workers, tests, scripts, and skills. Alembic migration handles mixed-format ID conversion (short hex, strings, existing UUIDs).

### Added
- **TaskStatus.BLOCKED + blocked_by_task_id** [hotfix]: Added `blocked` status to task state machine with `blocked_by_task_id` FK (self-referencing). Transitions: `in_dev → blocked`, `blocked → in_dev | backlog | cancelled`. `/implement` skill updated: auto-unblocks tasks when blocker is done. Migration, schemas (create/read/update), 3 new unit tests.


- **Story: priority + blocked_by fields** (task-9d288940): Added `priority` (int, default 0) and `blocked_by_story_id` (FK → stories.id) to Story model. Migration with indexes. Schemas updated (create/read/update). List endpoint gains `priority` filter and `sort` param. Validation: cannot start a story if its blocker is not completed (422). 8 new unit tests.

- **Story model + API** (wi-34761901): New `Story` entity (`id, project_id, parent_story_id, title, description, acceptance_criteria, status, created_by`). `StoryStatus` enum: `created | in_progress | completed | archived` with valid transitions. Full CRUD API at `/api/stories/` with action endpoints (`/start`, `/complete`, `/archive`). `Task.story_id` nullable FK. Self-referencing `parent_story_id` for epic-like grouping. Alembic migration. Refactored `list_tasks` to use `_TaskFilters` dependency class (PLR0913). 47 new unit tests.

### Fixed
- **Missing Project warnings spam**: `github_sync` worker now respects `GITHUB_ORG` env var instead of indiscriminately checking the first organization the GitHub App is installed in, preventing false `MISSING` alerts when installed in multiple orgs.
- **Admin notifications spam**: `notify_admins` now correctly filters out regular users based on the `is_admin` database flag instead of blasting messages to all users in the system.

- **Test Infrastructure Audit**: Fixed 10 bugs and warnings, optimized run speed.
  - Parallelized `make test-unit` execution in bash (35s → ~12s, 2.6x speedup).
  - Fixed unmocked `notify_admins` in scheduler unit tests.
  - Fixed missing `X-Telegram-ID` header in backend integration `seed_project` fixture.
  - Replaced 9 deprecated `HTTP_422_UNPROCESSABLE_ENTITY` with `_CONTENT` across API routers.
  - Disposed app DB engine in test teardown to fix 5 asyncpg `ResourceWarning` leaks.

- **Scaffold script task_description escaping** (#52): Pass `task_description` via copier `--data-file` instead of inline `--data` to prevent shell metacharacter injection (quotes, backticks, `$()`, parentheses). Base64-encode in Python, decode inside bash into YAML file. Added 9 parametrized tests for dangerous character patterns.

### Added
- **Repository model + migration** (wi-ad3b4502): New `Repository` entity (`id, project_id, name, git_url, provider_repo_id, role, is_managed`). Full CRUD API at `/api/repositories/` with `by-provider-id` lookup. `Task.repository_id` nullable FK. Alembic migration. 10 unit tests + 2 integration tests. `RepositoryRole` enum: `primary | dependency`. Documented `uv sync --reinstall-package shared` requirement in CLAUDE.md.

- **make sync — docs generation from DB** (task-94f2783f):
  - `POST /api/tasks/push` endpoint — auto-priority (`min(backlog) - 1`)
  - `source_brainstorm_id` filter on `GET /api/tasks/` for sibling lookup
  - `scripts/generate_status.py` — STATUS.md dashboard (current task, events, stats)
  - `scripts/sync_recent_artifacts.py` — plans/brainstorms window (in_dev + last 3 done)
  - `make sync` umbrella target (backlog + roadmap + status + recent-artifacts)
  - `make task TITLE="..."` CLI wrapper for quick task creation
  - Event writes in /implement: ci_fix, plan_deviation, implementation_summary
  - Event reads in /implement and /plan: resume context + sibling tasks
  - Cleaned 20+ stale plan files and 9 brainstorm files
  - Updated DEV_PIPELINE.md with full workflow docs

- **PR flow + in_ci status + need_e2e** (#64): Complete task lifecycle with CI and testing gates.
  - Renamed `IN_REVIEW` → `IN_CI` status; transitions: in_dev → in_ci → testing → done
  - Added `need_e2e` boolean field to Task model (controls smoke vs full E2E testing)
  - `/complete` endpoint auto-promotes through intermediate statuses (in_dev → in_ci → testing → done)
  - Rewrote `/implement` skill: push → PR → CI → smoke/E2E → merge → done
  - Updated `/e2e-run` skill URLs from `/api/tasks/` → `/api/runs/` post-rename
  - Alembic migration for in_ci status rename + need_e2e column
  - 10 new unit tests, 3 flow tests, service test updates

## 2026-03-07

### Changed
- **Rename WorkItem→Task, Task→Run** (#64): Full entity rename across codebase.
  - Planning layer: `WorkItem` → `Task` (table `work_items` → `tasks`, ID prefix `wi-` → `task-`)
  - Execution layer: `Task` → `Run` (table `tasks` → `runs`)
  - API routes: `/api/work-items/` → `/api/tasks/`, `/api/tasks/` → `/api/runs/`
  - Alembic migration renames tables and FK columns in correct order
  - All models, schemas, routers, workers, tests, scripts, and skill files updated
  - ~48 files changed, ~1950 insertions, ~1925 deletions

### Added
- **Milestone model + ROADMAP generation** (#63): Milestones as DB entities to group work items into phases/epics.
  - `Milestone` SQLAlchemy model (id, project_id, title, description, sort_order, status, parent_id, created_by)
  - `MilestoneStatus` DTO with transitions (open -> completed)
  - `POST/GET/PATCH/DELETE /api/milestones/` — CRUD endpoints with project_id/status filters
  - `POST /api/milestones/{id}/complete` — action endpoint with transition validation
  - `GET /api/milestones/{id}/work-items` — sub-resource listing
  - `WorkItem.milestone_id` FK — links work items to milestones
  - `?milestone_id=X` filter on work items list endpoint
  - Alembic migration for `milestones` table + `work_items.milestone_id` column
  - product-planning generation tooling
  - `scripts/seed_milestones.py` — one-time migration of existing ROADMAP phases
  - 33 unit tests (DTO, model, schemas, router, roadmap formatter)
- **Brainstorm model in DB** (#61): Brainstorms as first-class DB entities instead of markdown-only files.
  - `Brainstorm` SQLAlchemy model with status state machine (draft → done → triaged → archived)
  - `POST/GET/PATCH/DELETE /api/brainstorms/` — CRUD endpoints
  - `POST /api/brainstorms/{id}/done|triage|archive` — action endpoints with transition validation
  - `WorkItem.source_brainstorm_id` FK — links work items back to originating brainstorm
  - Alembic migration for `brainstorms` table + FK column
  - Updated `/brainstorm` and `/triage` skills to use API
  - 30 unit tests (DTO, model, schemas, router), 3 integration tests
- **Skills → API + Simplified Model** (#58): All skills now use Work Items API instead of markdown files.
  - `plan` text field on WorkItem model + migration
  - `COMMENT` event type (Jira-style discussion); removed `STEP_START`/`STEP_DONE`
  - `GET /api/work-items/stats` — status counts
  - `GET /api/work-items/next-tag` — next available backlog tag number
  - `GET /api/work-items/?since=<datetime>` — filter by updated_at
  - `project_id` and `plan` fields on `WorkItemUpdate` schema
  - `scripts/generate_backlog.py` + `make backlog` — generate backlog.md from API
  - `docs/ideas.md` — standalone Ideas file (read by make backlog)
  - Updated `/plan`, `/implement`, `/triage`, `/checkpoint` skills to use API
  - `/next` skill removed (absorbed into `/implement`)
  - 12 new service tests for API v2 endpoints
- **`/implement` emits work item events** (#57): `/implement` skill now writes `step_start`/`step_done` events via `POST /api/work-items/{id}/events` at each plan step, and calls `/complete` on task finish. New `step_start`/`step_done` event types in `WorkItemEventType`. `/next` now writes `WorkItem` ID to STATUS.md for downstream skills.
- **`/next` skill via Work Items API** (#56): First skill migrated from markdown parsing to API. `/next` now picks tasks via `GET /api/work-items/?status=backlog&limit=1` and starts them via `POST /api/work-items/{id}/start`.
  - `limit` and `sort` query params on list endpoint
  - `GET /api/work-items/by-tag/{tag}` — lookup by backlog tag (e.g. `#53`)
  - 5 service tests for the `/next` flow
- **WorkItem task management system** (#55): Planning layer for tracking features/fixes with agile statuses (backlog → todo → in_dev → testing → done). Models: `WorkItem`, `WorkItemEvent`. Action-based API with state machine validation. Alembic migration, 25+ unit tests, service tests, backlog migration script.
  - `POST/GET/PATCH/DELETE /api/work-items/` — CRUD
  - `POST /api/work-items/{id}/start|complete|fail|reopen|transition` — state machine actions
  - `GET/POST /api/work-items/{id}/events` — event history
  - `Task.work_item_id` + `Task.iteration` — links execution to planning layer
  - `scripts/migrate_backlog.py` — migrates backlog.md Queue into DB

### Fixed
- **Secrets not persisting**: `POST /projects/{id}/config/secrets` returned 200 but never saved. Root cause: plain `JSON` column didn't detect in-place dict mutations. Fix: `MutableDict.as_mutable(JSON)` on `Project.config` and `project_spec` columns + `dict()` copy in `merge_secrets` (#51)
- **Project stuck in "deploying"**: deploy-worker didn't reset project status on `missing_user_secrets`. Now rolls back to `failed` (#51)
- **API service tests event_loop**: replaced deprecated `event_loop` fixture with `asyncio_default_test_loop_scope=session` to fix "Future attached to a different loop" errors (#51)
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

### Fixed
- Corrupted checkpoint recovery: PO consumer auto-repairs orphan tool_calls that block users permanently (#48)
- `ruff.toml` per-file-ignores now covers `**/tests/**` paths (services tests were getting PLR2004 false positives) (#48)
- Race condition in `set_project_secret` when LLM calls it in parallel — secrets no longer lost (#47)
- `test_post_projects_pure_db` integration test — add `X-Telegram-ID` header and seed user via API (#42)

### Changed
- `set_project_secret` PO tool uses single POST instead of GET→decrypt→merge→encrypt→PATCH (#47)
- `_save_secrets_to_project` in devops nodes delegates to `api_client.merge_secrets` (#47)
- `owner_id` on projects is now NOT NULL — every project must have an owner (#39)
- `POST /api/projects/` returns 400 if `X-Telegram-ID` header is missing (#39)
- `github_sync` no longer creates orphan projects — sends admin notification for unknown repos (#39)
- Webhook removes `if project.owner_id` guard — owner always exists (#39)
- `ProjectDTO.owner_id` is now `int` (was `int | None`), `ProjectRead` includes `owner_id` (#39)

### Removed
- `ProjectUpdate.owner_id` field — owner is immutable after creation (#39)
- `SchedulerAPIClient.create_project()` — scheduler no longer creates projects (#39)

### Added
- Workspace failure counter: tracks consecutive failures per project in Redis (#8)
- Force workspace wipe after 2 consecutive failures — broken state auto-recovery (#8)
- Spawn rejection after 3 consecutive failures — circuit breaker with auto-unblock (TTL 48h) (#8)
- `reason` field on `DeleteWorkerCommand` — `completed`/`failed`/`timeout` for failure tracking (#8)
- `--feature` mode in e2e-run skill: triggers `action=feature` after initial create+deploy, verifies no scaffold, monitors feature CI+deploy (#34)
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

### Changed
- DeployerNode reads SSH key from DB (per-server) instead of mounted file (#33)
- `run_ssh_command()` accepts `ssh_key` content parameter instead of reading from `Paths.SSH_KEY` (#33)
- `docker-compose.yml`: parameterized `SSH_KEY_PATH` and `GITHUB_APP_PEM_PATH` with dev defaults (#32)
- `.github/workflows/deploy.yml`: complete rewrite — writes all env vars, builds images, pulls worker images from GHCR, runs migrations, health checks (#32)

### Removed
- SSH volume mounts (`~/.ssh:/root/.ssh:ro`) from langgraph, deploy-worker, scheduler, infra-service (#33)
- `Paths.SSH_KEY` from `shared/constants.py` — no longer needed (#33)
- `ORCHESTRATOR_SSH_KEY` secret from deploy.yml — per-server keys in DB now (#33)

### Fixed
- CI: service test matrix `changed` field was literal string, not `${{ }}` expression — tests were silently skipped on every run since #4 (#38)
- API: make `X-Telegram-ID` optional for project creation — system calls (scheduler github_sync) create discovered projects with `owner_id=None` (#38)
- Service test `test_pure_crud`: removed unnecessary `X-Telegram-ID` header (test verifies no side effects, not ownership) (#38)
- Service test `test_service_db_smoke`: fixed event loop mismatch caused by session-scoped DB engine (#38)

## 2026-03-05

### Fixed
- Atomic port allocation: `UniqueConstraint(server_handle, port)` + `POST /ports/allocate-next` endpoint with `SELECT FOR UPDATE` — eliminates TOCTOU race in parallel deploys (#31)

### Removed
- Dead `ports.py` PO tools (`allocate_port`, `get_next_available_port`) and `PortAllocationResult` schema — replaced by atomic allocation in `allocator.py` (#31)

### Fixed
- Multi-user isolation: PO tools now pass `X-Telegram-ID` header in all API calls (#30)
- API requires `X-Telegram-ID` for project creation — prevents orphan projects with `owner_id=NULL` (#30)
- Workers pass user's telegram_id to API when fetching projects, enabling ownership checks (#30)
- `LanggraphAPIClient.get_project()` and `list_projects()` accept optional `telegram_id` param (#30)

### Changed
- Replaced last "Zavhoz agent" reference with "ResourceAllocatorNode" in `AllocatedResource` docstring (#12)
- Clarified engineering-worker and deploy-worker as Redis stream consumers of the langgraph image, not independent services, across CLAUDE.md, README.md, ARCHITECTURE.md (#12)
- CI integration tests: sequential → 5 parallel matrix jobs (backend, cli, template, frontend, infra) (#4)
- Per-suite change detection: each integration suite only runs when relevant files changed (#4)
- Healthcheck intervals 5s→2s in non-DIND test compose files (frontend, infra, cli) (#4)
- Per-suite Docker buildx cache keys for better cache hits (#4)

### Removed
- Dead `list_repos.py` debug script from langgraph service (#17) — 72 LOC, standalone script with `sys.path` hack and `print()`
- Legacy name-based project lookup fallback in github_sync (#17) — `get_project_by_name` from scheduler API client + fallback in `_sync_single_repo`
- Dead CLI agent config infrastructure (#36): `CLIAgentNode`, `cli_agent_config_cache`, CLI agent config API router/schema/ORM model, alembic migration — 423 LOC deleted
- Dead `architect_complete` field from `OrchestratorState` and provisioner init (#37)
- Vestigial references to removed agents (architect, Zavhoz, product_owner, brainstorm, developer) in comments/docstrings (#37)

### Fixed
- Fail fast with `RuntimeError` when `ORCHESTRATOR_USER_ID` not set in CLI commands (#29) — was silently defaulting to `"unknown"`, breaking audit trail

### Changed
- Defensive init `smoke_result: None` in `_build_subgraph_input` (#25) — consistent with other Optional fields
- Diagnostic logging `devops_subgraph_result` in deploy_worker after `ainvoke()` — for #25 root cause investigation
- Updated `/e2e-run` skill to check deploy-worker logs for smoke diagnostics

### Added
- E2E report: todo_api with-PO mode PASS (12 min) — first test with PO creating project via Redis Streams
- Post-deploy smoke tester node in DevOps subgraph (#25): HTTP `/health` check for backends, Telethon `/start` check for tg_bot modules
- `SmokeTesterNode` with retry logic (3 retries, 5s delay) and graceful skip when Telethon not configured
- `smoke_result` field in `DevOpsState` — propagated through deploy_worker to task result
- Conditional routing: `deployer` → `smoke_tester` → END (skips smoke on deploy failure)
- Telethon dependency + env vars in deploy-worker compose config
- Updated `/e2e-run` skill to report smoke results

### Changed
- Extract `infra_client.py` (279 LOC) from langgraph + infra-service to `shared/clients/` (#23)
- Merge duplicated constants (`Paths`, `Timeouts`, `CI`, `Provisioning`) into `shared/constants.py` (#23)
- Service-local `config/constants.py` now re-exports from shared (#23)
- Add `shared/tests/**` to ruff PLR2004/S101 per-file-ignores (#23)
- Restructure ROADMAP: split Phase 2 → 2A (pre-MVP alpha blockers) + 2B (post-alpha stability)
- Triage: 7 new tasks (#30-#35), reopened #25 as regression, reordered backlog by roadmap phases
- New brainstorm: epic decomposition — decision: Task Store in DB (Phase 3), skip intermediate file-based epics
- Triage skill: added Queue reorder step based on ROADMAP phase priorities

## 2026-03-04

### Added
- Auto-detect stale worker images: source hash label in `worker-base-common`, `check-worker-images` target in Makefile, auto-rebuild in `make build` and E2E pre-flight
  - Root cause: `POSTGRES_HOST=project-db` bug persisted 4 E2E runs because `shared/` fix was never baked into worker image ([worker audit](e2e_results/todo_api-20260304-levelC-worker.md))
- LangGraph integration tests (#6): 3 tests against real DB/Redis/API (engineering worker flow, missing project, scaffold_failed abort)
- Engineering-worker service in backend test compose
- API data seeding fixtures (`seed_project`, `seed_task`, `seed_server`) + `poll_task_status` helper
- E2E reports: todo_api Level C PASS (14 min), weather_bot Level C PASS (15 min, first multi-module test)

### Fixed
- Enforce fail-fast for env vars (#24): notifications.py uses lazy init — import safe, first call raises RuntimeError if TELEGRAM_BOT_TOKEN/API_BASE_URL missing
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

### Changed
- Unify ServerStatus enum, remove dead IncidentDTO (#15)
- Consolidate ServiceModule enum, remove dead code (#16, #17)
- Sync worker prompts with simplified service-template (#1)
- Migrate `(str, Enum)` to `StrEnum` across codebase (21 instances, 14 files)
- Remove deprecated `update_framework` command
- Remove stale ruff.toml per-file-ignores
- Deduplicate MockProcess into shared test conftest

### Added
- Refactor audit v2 report

## 2026-02-23

### Fixed
- Pin fakeredis>=2.34.1 to eliminate deprecation warnings
- Timezone=True for Task model datetime columns
- Healthcheck intervals tuned, worker-manager lock refresh

### Added
- E2E testing skills for Line 2 engineering pipeline (e2e-run, e2e-check, e2e-cleanup)

### Fixed
- Scaffold skip bug, description passthrough, CI gate 404 handling

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
- Deploy architecture (9 iterations): Fernet encryption, env groups, GitHub Actions deploy, webhook auto-deploy, self-hosted Docker registry + Caddy TLS
- PO ReactAgent migration: CLI subprocess → async LLM consumer with reminder polling and direct tool access
