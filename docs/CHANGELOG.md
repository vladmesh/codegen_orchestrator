# Changelog

One `## YYYY-MM-DD` heading per merge day, newest first; one bullet of at most two lines per entry.
See the CHANGELOG rule in [AGENTS.md](../AGENTS.md).

## 2026-10-08

- Telegram injection fixtures use a valid one-second wait and a virtual child clock,
  preserving their security checks under the bounded probe wait contract.

- QA Telegram probes accept waits up to 60 seconds; invented inputs and server-confirmed missed replies
  settle as unverified QA tooling checks instead of product failures.

- Production deploy-worker reaches platform auth over its external link; deploy validates admin secrets,
  Promtail collects platform logs, and the runbook documents token rotation and verification.

- Deploy issues and persists platform keys before create-only auth registration, preserving operator grants;
  revoked keys rotate and unavailable auth retries without asking the user.

- Public channel content is planned via platform-backed catalog modules only; product scraping is forbidden.
  Bot briefs default to RU/EN; new bots stage module installation after the base story.

- Catalog-install capability text derives its core version from the pinned kit tooling,
  so the Architect's static install rule matches catalog admission and product preflight.

## 2026-10-07

- Scaffolding and tooling use kit 0.10.0/core 2.4; env contracts match its platform sources,
  which explicitly refuse deploy until configured instead of asking the user for a secret.
- Products scaffold from kit 0.8.1, whose generator reformats after its lint fixes, so a bound product's `bindings.py`
  passes its own `ruff format --check` (stand-e2e run 37579868223); tooling pins move to `ecdab3af`.
- Production/stand scaffolder `mem_limit` is 2g, not 512m: catalog install's cold product mypy ran past
  the 600 s command timeout at stage validate (stand-e2e run 37562802977).
- The mechanical stand's notes router casts its redis 7.x calls to `Awaitable[T]`: once install's product
  mypy finished, it refused the bare awaits at stage validate (stand-e2e run 37567975527).
- The mechanical stand's install story stops its deploy wait when the story parks and keeps its
  quarantine and PR/CI observations: run 37572801062 waited 22 min on a story parked at 05:05.

## 2026-10-06

- Catalog install runs the product unit leg with `REDIS_URL=redis://redis.invalid:6379`; its exported
  `.env` points it at the orchestrator Redis; the stand validate stage hung to timeout (run 37516572347).
- A timed-out catalog install command refuses with its argv instead of an empty detail.
- Catalog install points venvs the worker repointed at `/workspace` back at its own checkout;
  `.venv/bin/kit` failed with ENOENT on the `/workspace` interpreter (run 37502715758).
- Catalog install trusts its workspace via `safe.directory` in the product and install git env;
  root scaffolder git refused the worker-owned (1000:1000) checkout as dubious (run 37461078840).
- Mega-noop QA keeps the probe's inner failure (`IdentityNotProven`, reason, redacted detail) in
  `failure_cause` and the check detail; it was overwritten as `ProbeFailure`.
- Live cleanup waits 120 s, not 30 s, for a project's active work to stop; a ~57 s workflow cancel
  in run 37445448651 failed teardown.
- Host unit profile: 0.5 s per-test budget, a CPU budget in `python -m shared`, `subprocess`/`slow`
  markers, no real waits; CI uploads junit and durations. AGENTS.md test rules; ~110 useless tests gone.
- Unit tests may not read `docs/`, repository Markdown or `inspect.getsource` (`make lint`), and the
  PO-tools and `ChatOpenAI` import boundaries are ruff `TID251` rules instead of tree-grep tests.
- Mega-noop checks the QA grant's `head_sha` against the story commit its deploy Run targeted,
  not the merge commit; a passed notes probe no longer fails the `grant` phase.
- QA admission treats one `Stand mechanical notes|reminders` probe line beside GET checks as
  deterministic, as the QA consumer does; mega-noop no longer stalls without a QA executor.
- Mega-noop notes change set mounts its router inside `router.py`'s import block (E402 failed the
  product `pre-commit`); a kit-gate test now runs the hook's `make format` over that exact set.
- A gave-up worker's failed-step output tail (redacted, ≤2000 chars) is appended to the blocked
  reason, so `failure_metadata.reason` and stand evidence show what a hook actually said.
- `redact_diagnostic` bounds the URL scheme to 32 characters, so redacting a long hex or base64
  stderr tail is linear (100 kB: tens of seconds to milliseconds); unit tests parse the kit catalog once.

## 2026-10-05

- `python -m shared` runs the light `--host` profile (2 jobs, `-m "not ci_only"`, no live-offline);
  docker, ansible, sudo and kit-gate tests are marked CI-only and a guard scan keeps them there.
- Mechanical notes and reminders briefs use the released usage-example fields; every `Level1Brief`
  now validates as a Product Brief proposal when built, so a refused document fails offline.
- The stand handoff and acceptance artifact carry `mechanical-install-<run_id>.json`, so a
  `mega-noop` run keeps its redacted partial-failure facts.

- Mechanical stand provenance uses the workflow's synced SHA because bootstrap excludes `.git`;
  partial failure artifacts no longer abort on Git metadata absent from the stand.

- Mechanical QA reconciles the exact due reminder after actual Telegram arrival, so the
  producer's later emission confirmation cannot cause a false stand failure.

- `mega-noop` proves native reminders installation into persistent notes, with owned planning,
  real Telegram receipt/delivery/cancellation and redacted release evidence without model turns.

- Install Git bypasses product hooks with scoped credentials; anonymous product checks,
  protected bot entrypoints and a shared GC lock preserve owned notes workspaces.

- Catalog selections persist one typed install; scaffolder validates and publishes its owned
  story head with released kit 0.8.0, preserving product files without paid engineering.

## 2026-10-04

- The unit runner, compose runner and stand render test pass only the three Ummanu docker-guard
  bindings when set, so read-only `docker compose config` tests pass on the control host.
- Worker output receipts expire after a 24-hour replay window and owned teardown
  collects every lease, including legacy receipts, to bound retention without duplicate delivery.
- Deploy cleans only scoped worker/service releases, retaining rollback and container-used images;
  foreign worker descendants and unowned dangling images stay on the shared daemon.
- DinD publishes its no-model Factory turn through native typed attempt authority
  and proves broker output acceptance/ACK, so repository refusal reaches the shipped wrapper.
- DinD proves standalone attempt identity and isolates native queue attribution
  from a competing engineering consumer, so worker creation cannot race fixture settlement.
- DinD worker fixtures persist and read back eligible attempt authority; scoped
  cleanup retains startup evidence so real worker gates reach their intended boundaries.
- Recovery validates copied objects in a clean manager repository before minting;
  verified handoff retires its publication holds so later operator actions remain usable.
- Empty-result retention compares and persists the typed outcome, so omitted optional
  fields cannot block honest settlement or replay while changed facts still refuse.
- Native actions share cause-aware stop release; retained empty outcomes require
  exact accounting at settlement, while standalone infrastructure parks keep their own refusal reason.
- Explicit recovery selects its stop; empty-result reclaim retains paid facts,
  and API launch coverage includes image-only variants so stop fencing preserves authorized actions.
- Test API containers explicitly configure worker-manager connectivity, so service
  and integration CI can start the publication recovery API without a production default.
- Unpublished worker commits park for explicit agent-free recovery; committed
  engineering stops fence retries, worker turns and late deploy claims, preserving paid outcomes.
- Health QA's product HTTP fixture matches the root path explicitly, so its
  reachability response cannot shadow caller identity proof and health reads in CI.
- Health QA service fixtures use canonical diagnostic reasons, and the LangGraph
  test image includes its HTTP mock dependency, so required CI reaches the boundary tests.
- CI exercises health-only QA admission and persisted terminal routing without model
  sessions, including unhealthy and refused endpoints, to close the hotfix's boundary evidence gap.
- Health-only QA handoffs skip unused model diagnostics at admission, so a stand
  without model profiles can finish deterministic QA instead of quarantining the story.
- Raw stand targets retain model sessions and profile protection unless they resolve
  to a registered no-model suite, so paid custom probes fail closed before provisioning.
- Stand noop skips model sessions and authentication throughout setup and preflight;
  paid suites retain refresh/redaction checks, and SSH failures preserve runner evidence.

## 2026-10-03

- Repository auth parks offer the existing admin retry; gh permits auth-valued arguments,
  and Codex developer shells inherit only process settings and the scoped broker identity.
- The Git credential helper accepts native repeated array attributes while rejecting ambiguous
  repository fields, so Git 2.55 can acquire credentials for checkout and publication.
- Developer Git/gh acquire repository credentials on demand, so reused workers and long turns
  survive token expiry; pre-agent auth refusal parks as infrastructure without model spend.
- Products scaffold from kit 0.7.1, whose template lifespan tests no longer start package runtimes, so a product
  that runs `kit add reminders` keeps a green CI unit leg without Redis; both tooling pins move to `56da5c83`.
- mega-noop's second story installs `reminders` with a new `@@ kit-add` directive; health-only QA
  reads as the verified QA identity, so the suite proves the install, `/reminders` and the timer's emit.
- The Architect plans kit packages from the kit's live catalog, not a static list, and `create_task`
  refuses a non-catalog `kit add` or a wheel, so a package release needs no orchestrator change.
- Every deploy write checks the project deploy lock's holder token first; a deploy that lost it ends
  `deploy_lock_lost`. The retry bound counts `supervisor_retry` Runs since the last success, not a TTL key.
- [hotfix] Stand control-plane downloads (Docker key, uv) wait 60 s per attempt with 5 bounded retries,
  so one slow read no longer fails the after-merge stand bootstrap (run 37085191952).

## 2026-10-02

- Central QA reads kit 0.7.0 package routes as one granted QA user via `http_get` identity headers;
  call results and retained evidence are scrubbed of one set holding every QA run secret.
- Products scaffold from kit 0.7.0 (core timer, verified caller identity, package catalog); workers install
  packages with `kit add <name>` from the live catalog instead of hand-building a wheel at `_commit`.
- Old product name swept: AGENTS/TESTING name Ummanu and its worker-packet broad check, the BitLaunch helper
  reads `~/ummanu/.env`, and `.gitignore` ignores `ummanu-data/`.
- The deploy `.env` ends in a newline: a generated workflow's appended `PUBLIC_BASE_URL` glued onto the last
  variable, `USERS_GRANT_CAPABILITY`, so every initial-owner grant got 403 `grant_rejected` (story-92b433c8).
- Conflict repair attempts count started Runs: a paid refusal keeps the Task todo and stops only the Story with "no budget"
  (limit, spend), so the same repair command resumes it; a migration unsticks released refusals (c351).
- [hotfix] The scaffolder image links copier into /usr/local/bin again (#694 left it off PATH, so every scaffold
  failed); the service-image check now fails an image missing an executable its service runs by name.

## 2026-10-01

- Bump PyJWT to 2.15.0, urllib3 to 2.8.0 and brace-expansion to 5.0.12 in locks and pins, closing the open Dependabot
  alerts; also takes the frontend patch bumps of Dependabot PR #675.

- `POST /applications/{id}/monitoring` switches health probing and alerts off per application without touching status or ports;
  SERVICE_DOWN incidents are now matched by application id, so a healthy sibling no longer closes them (#633, #632).

- Split the monolithic contracts reference into a focused index and boundary guides, and refresh stale architecture/agent/secret docs so default context stays small and current.

- Historical changelog detail before 2026-09-22 is compacted to product milestones; recent ten-day entries stay granular so the file remains useful without becoming required historical context.

- Align database migrations with ORM metadata and enforce Run type/status at API and database
  boundaries, preserving incident uniqueness and rejecting invalid persisted state before migration.

- Retry the stand e2e `CODEX_AUTH_JSON` secret write up to five times, so a transient GitHub 5xx
  does not fail the run or lose the refreshed Codex token.

- Make LK bearer authorization deny-by-default: valid dashboard tokens reach only routes with an
  explicit owner/admin/current-user bearer guard; unclassified routes remain internal-only.

## 2026-09-30

- Quarantine malformed and repeatedly failing Redis work before ACK, preserve transient failures for
  bounded reclaim, and propagate cancellation so queue consumers no longer lose or loop poison entries.

- Persist scheduler deploy Runs and exact queue handoffs before Story transitions, with stable attempt IDs and queued-handoff recovery so dispatch failures cannot strand DEPLOYING stories.

- Report every LangGraph service-test setup, call and teardown duration to measure native
  notification waits separately from fixture and CI overhead.

- Overlap four native initial-owner notice claim intervals in service tests, preserving separate
  route results and failure cleanup while removing three repeated minute-long waits.

- Combine reviewed Python and frontend dependency fixes; pin PyJWT 2.15 in the API image
  to reject malformed JWT claims, and run admin frontend tests on the image's Node version in CI.

- Finish scheduler-pipeline decomposition: engineering dispatch, scaffold, story completion, lifecycle
  supervision and QA routing now run behind independent failure boundaries.

- Run merged-PR handling and CI-failure routing in their own scheduler loop, so dispatcher failures
  cannot delay deploy routing and one PR/CI path cannot suppress the other.

## 2026-09-29

- Admit provider-absent, unreachable, unmanaged and off-allowlist Time4VPS history in production preflight;
  retain blocking drift for live inventory, authorized targets and unsettled rows.

- Verify a policy increase on an exhausted Story through lifecycle replay, proving no automatic
  reset without asking the deploying scheduler to process a terminal Story.

- Revalidate initial-owner retry against current deploy policy at locked API read and command boundaries;
  keep terminal Story and notice wording stable so policy changes cannot promise unavailable retry.

- Derive initial-owner retry guidance from the verified exhausted Run across readback, notices
  and PO capabilities: zero admissions offer no action, while fenced terminal Runs retain it.

- Assert zero-ceiling replay preserves the exhausted intent and notice episode while reporting
  the per-call created flag accurately, so CI distinguishes replay from first intent creation.

- Recover owed initial-owner Run publication through the deploying supervisor; stop trusted zero-admission
  and typed cancelled exhaustion, and refuse premature human retry without resetting its epoch.

- Correct initial-owner CI notice, cancellation and replacement-owner fixtures, and retain the
  recovery deadline without a pytest plugin so native retry proofs can execute.

- Allow owner/admin retry of exhausted initial deployment with an immutable attempt fence;
  commit its typed Story cause and both owed notices so automatic recovery stays bounded.

- Compare conflict refusal history by immutable event IDs and validate both refusal audits, preserving
  complete records when atomic retry events share a timestamp and the API returns a different order.

- Dispose no-Run conflict dispatch refusals with their Task, Story and both notices atomically;
  fence deliberate resume with immutable refusal and retry-bound authority.

- Read physical Run metadata in the late-start recovery fixture so CI can verify the next admitted
  attempt and continue bounded exhaustion after the delayed start.

- Fence admitted conflict starts with immutable Run and settlement evidence, so delayed dispatch
  cannot overwrite an atomic retry or revive settled work.

- Recover terminal conflict Runs through scoped settlement before generic Task replay, so interrupted
  refusals owe both notices and immutable Run evidence decides the bounded ending.

- Reconcile reclaimed checkout baselines before worker turns; atomically fence conflict retries and
  terminal settlement to their admitted cycle and Run so lost responses cannot strand or stop new work.

- Advance merged story branches without discarding local work; admit one bounded dirty-PR repair
  through normal dispatch, including authenticated recovery of the released dirty quarantine.

- Atomically fence every leased ACK retry with ownership and retained result settlement; expose lost-reply
  ambiguity and provision the required Telegram concurrency policy on the stand.
- Telegram dispatches users concurrently while retaining per-user order; live work survives short Redis
  outages within confirmed lease ownership and refuses expired renewal.

- Bind nightly backup and independent readback to h01o's owning rootless user/daemon;
  install private policy and timer in that user's manager so production proof uses the right database.
- Publish verified private PostgreSQL archives and gate production Switch before migrations;
  preserve protected backups across rotation and rollback, and pin runners to Ubuntu 24.04.
- Keep mapped HTTP hosts stable across Python patches and reconcile rejected workflow customizations
  in released-kit upgrade checks so CI exercises the same deployed endpoint and reviewed update.
- Validate effective mapped self addresses and IPv6 smoke; adopt released kit transport and refuse
  unverified existing workflows until a reviewed update so accepted endpoints can finish deployment.
- Derived `PUBLIC_BASE_URL` uses the deployed HTTP endpoint; resolver failures reach owners,
  and verified merged repairs open bounded grant epochs without duplicate concurrent dispatch.

## 2026-09-28

- Empty-result settlement preserves its cause across exhausted siblings and interrupted writes;
  committed stops resume remaining task/Run effects without another worker turn or notice episode.
- Empty engineering and no-commits PR stops persist their reason and owed owner/admin notices
  atomically; planned-task retries keep their budget and notify on exhaustion.
- Backend DinD fixtures provide matching origins and synthetic repository credentials; explicit
  `/tmp` mounting preserves shared workspaces so real worker credential preparation can run.
- Filtered agents use private HOME Git configuration; recovery transfers remote credentials as
  private files and cleans them after clone, update or failure.
- Recovery uses private Ansible vars and redacted diagnostics; worker preparation upgrades
  credentialed origins and keeps Git auth outside reused workspaces.
- New products scaffold from kit `0.6.3`, carrying bigint user identifiers and Telegram token-safe
  logging; the generated fixture and capability provenance follow the published release.
- PO Redis/checkpoint maintenance shares one Deploy order, fences writes through readiness and
  reconciliation, and drains late proactive work before switching images.
- PO Redis dialogue, DLQs, reminders and owner-event caches encrypt whole payloads before writes;
  a quiesced upgrade preserves retained evidence and delivery state.
- PO checkpoints encrypt all dialogue payloads with the project key; a quiesced, atomic upgrade
  preserves existing conversations and pending work with count-only reporting.
- The checkpoint upgrade runbook uses the complete production image chain and stops PO writers
  again after Deploy, so conversion runs from the released digest with ingress paused.
- Capability manifest v5 is the owner's product list: the PO reads a product-only block and refusals quote
  only product fields; the Architect gets the technical block; payments, backups and web presence detected.
- The API refuses to start with an empty `INTERNAL_API_KEY`, and an empty `X-Internal-Key` never
  authenticates, so a blank key can no longer be an internal-service bypass.
- Live-run cleanup releases the PO's `po:latest_owner_event:*:<story>` records of the run's stories,
  so the residue proof no longer fails on them (e2e run 36383692107).

## 2026-09-27

- A commit whose env contract requires a derived key the resolver cannot compute fails engineering
  with each key named, before any deploy; capability detect terms no longer trip on nearby words.
- Intake and Architect admission refuse unsupported capabilities unless the user accepted a
  manifest workaround; insisting requests go to admins, and scheduled work needs a product timer.
- The deploy-target permission test creates its disposable user in bounded setup with an empty
  skeleton and no login-log initialization, isolating account setup stalls from the role proof.
- The deploy-target test pins its Python and pipelines local modules to reduce runner-dependent
  startup work; verbose failure output identifies the command stalled inside a role task.
- The deploy-target permission test skips unused fact gathering and bounds each Ansible apply at
  30 seconds, reporting captured output and elapsed time before pytest's timeout.
- `docs/platform_capabilities.yaml` (v1, draft) states what a product can and cannot have; PO and
  Architect read its compact block, and a test fails when a derived key or port service is unlisted.
- Briefs record chosen variants, quality trade-offs and later alternatives; both views show them,
  and planning explicitly builds only the chosen variant.
- A state-age pass that ends more waits than `supervisor.state_age_mass_park_threshold` marks each
  `mass_sweep` and sends admins one message; the PO snapshot tells it as a late notice after downtime.
- A stopped story is told as "stopped, a person is needed, no known deadline"; the PO prompt forbids
  "tested", "standard check/procedure" and "a specialist is checking" wherever it tells a stop.
- The PO publish point logs a reply withheld for a replaced or settled notice; a notice record gone
  before its admin copy is marked sent answers 410, not 500.
- The first-boot settle unit test waits for every process of its session before removing its temp
  dir, so a stub killed by `timeout` can no longer race the cleanup (CI flake).
- PO can defer owner notices with a reason and an admin copy, retrieve them until told or closed,
  and record telling separately from queue delivery.

- Every PO system-event turn carries a code-built situation snapshot (order date, wait age, app health, other orders)
  as model input only, never checkpointed; `get_product_situation` answers the same on request.
- `stories.status_entered_at` is stamped by the one status writer, so the PO snapshot's wait age survives
  unrelated story writes; rows landed before it read `unknown` (migration `a4c6e8f0b2d5`, no backfill).
- An automatically retried planning is in work, not stopped; `set_reminder` names its story, a story-less reminder
  speaks only if the user asked, and a reminder about a not-ordered story runs no PO turn.
- PO publishes only key story changes: progress reminders and stage notices stay silent; every unreadable
  brief leaves its event pending instead of acknowledging an unknown audience.

- Only an ordered story's (confirmed brief) events reach the user, the rest go to the admins; the chat PO has no
  fix stories: a retry or a complaint reopens the original story.
- A reminder about a story reaches its owner only on an untold `needs_user` or `stopped` state; stage notices are
  dropped, so the PO's self-reminders no longer repeat "work is going" every 15 minutes.
- A stay in one stage is re-announced at 1, 2, 4, 8… quiet intervals, each gap capped at
  `supervisor.stage_notice_max_interval_minutes`; a notice carries its step and stay, told only in rising order.
- `notify_user` sends nothing in a reminder or system turn, so the gated final reply is the only way such a turn
  reaches the user.
- [hotfix] `LLMAlerts.drain` no longer busy-loops on an alert whose done callback is still queued (the unit
  hang on main); every unit test has a 90 s timeout that names a hanging test and its asyncio tasks.
- RAG is removed: its routes, embedding client, ingest, summarizer, Telegram message capture and four `rag_*` tables
  (dropped by migration `d3f5a7c9e1b4`) had no reader; `OPEN_ROUTER_KEY` is no longer required.
- Stand live test `test_llm_channel_failover.py` (custom target) proves Architect and PO failover healthy, with codex
  faulted and with both subscriptions faulted, faulting only agent-config chains and restoring them always.
- [hotfix] Live-run cleanup deletes the PO reminder gate's `po:story_told*` keys of the run's stories, so the
  residue proof no longer names the gate's TTL-bound per-day counters.
- [hotfix] `mega-live`'s location proof accepts an out-of-range location check reported not applicable only on the
  seed probe's own argument refusal of the value it names, and records that refusal kind in the run evidence.
- [hotfix] `run_evidence` imports the location proof only to judge a read QA Run record, so the backend DinD suite,
  which has no `scripts/` on its path, imports it and builds artifacts again.

## 2026-09-26

- Administrators hear of an LLM channel 402, both subscriptions down (the PO then tells users capacity is out) and
  an OpenRouter balance below `llm.openrouter_balance_alert_usd`, deduplicated in Redis per channel or agent.
- A 402 alerts whatever class its text earned, and the balance check reads `/credits` with the optional
  `OPENROUTER_MANAGEMENT_KEY` (langgraph only), else the PO key; a refused read says to set it.

- A failed Architect planning is a story state (`stories.planning`, `planning_failed`): retried with backoff up to
  `supervisor.story_max_architect_retries`, parked at once when no retry can help, re-run by `retry-planning`.
- `stories.planning` is the durable record that planning is owed: `retry-planning` only writes it due now and the
  supervisor alone publishes it; a job arriving before its retry is due settles; `admit` records the channels.
- The "already decomposed" skip counts only admitted tasks or a live attempt's, never cancelled ones or a failed
  attempt's leftovers, so a re-queue reaches the claim; the PO reports a failed planning as a problem.
- The langgraph image carries Codex and Claude Code at the worker pins, so the codex and claude channels answer in
  langgraph and architect: codex on the workers' own profile, run as its owner under the shared lock.
- Each agent logs `llm_channel_ready` per channel at startup, and a PO or summarizer turn gives a subscription CLI
  180 s before moving on; the deploy writes the optional `CLAUDE_CODE_OAUTH_TOKEN` secret.
- The Architect, PO and PO summarizer answer through a per-agent LLM channel chain (`agent_configs.llm_channels`,
  default codex → claude → openrouter); a channel failure moves the same call on and every call logs its channel.
- An engineering success whose commit adds no change over the story branch head recorded before the attempt fails
  `no_new_commit` and retries as a task iteration, so a no-op is never `done`; the default-branch guard folds into it.
- A reused story worker handed another task, or a retry after a no-change attempt, gets `clear_session` and a TASK.md
  naming the task, so it no longer resumes the previous task's conversation and reports that task's commit.
- QA capabilities live in one catalogue (`shared/contracts/qa_capabilities.py`) that renders the Architect, PO brief and
  QA executor guidance and the pre-QA HTTP-write set; Telegram media, location, contact, reply and edit are now checkable.
- A check QA cannot run is recorded as unverified, not failed: the verdict comes from the checks that ran, so a
  capability gap no longer quarantines the product or parks the story.
- The settling owner event carries `qa_verification` (passed and unverified checks), and each unverified check is
  kept as a project verification gap (`GET /api/projects/{id}/verification-gaps`).
- The Architect rewrites or returns a must-requirement QA cannot check (`not automatically verifiable:` reason), and
  PO tells the user what QA could not check and records their answer on the story (`unverified_decisions`).
- The bot sends every user-bound text through `send_text`: split on `MESSAGE_BREAK` and under 4000 units as valid
  HTML, retries resume at the failed chunk, and errors reply a fixed apology, never "Message is too long".
- The Product Brief the user signs is one short message (≤ `BRIEF_MESSAGE_BUDGET` 3500): bold sections, each wording
  once, no ids or fillers; the full form is `show_full_brief` and `GET /api/product-briefs/{id}/full`, one section each.
- An over-budget brief opens no revision and writes no pointer; the PO stages the product instead of squeezing it, and
  proposals are capped in counts and lengths so the worst-case full form stays under 12k characters.
- `telegram_probe`/`telegram_click_button` are proven injection-safe by running their scripts on hostile values, and
  without a proven identity they return the missing-credentials blocker and start no child process.
- `show_full_brief` answers a fixed apology in the brief's language on any read failure, and the PO graph refuses the
  other calls of its turn, so the user gets the full brief or the apology, never another tool's result.
- `mega-live`'s bot also answers a native Telegram location, and `test_qa_passed` requires QA to prove it with a retained
  sandbox probe (passed, not unverified); `mega-noop` renders byte-for-byte as before, pinned offline.
- Live API clients drop idle connections after 1 s (uvicorn keeps 5 s) and retry a GET/HEAD/OPTIONS once on a dropped
  connection, never a POST, so a poll no longer dies on `Server disconnected without sending a response`.
- Live run cleanup removes the scheduler's `story:stage_notice:<id>` markers and set membership of its own stories
  before the residue proof, so an aborted run no longer fails `prove_nothing_left` on them.
- Live capability-stream cleanup rescans until clean (up to 6 scans, 2 s apart), logging and recording owned entries the
  platform publishes during teardown, e.g. the temporary-access revoke; only lasting residue fails the run.

## 2026-09-25

- Passed QA runs keep their exit-0 probes in a per-project library (cap 50), offered to later runs with platform seeds
  under `/workspace/qa-library`; the first seed sends a Telegram location with coordinates checked as floats.
- Probe library names are canonical at store time (non-matching names are skipped and counted), and a library that
  cannot be built degrades the run to seeds with a `build_failure` note instead of failing it.
- `qa probe` bounds each text by its encoded size, so a control-character-heavy probe fits the 256 KiB body limit.
- QA probe capture now survives timeouts, invalid bytes, output bounds and non-JSON endpoint failures; endpoint fields and request bodies are bounded, and QA workers retain no transcript mount.
- QA executor probes now retain scrubbed, bounded source and results on their Run, and QA removes its broker output
  stream rather than naming a deleted transcript file.
- The QA executor is a sandbox: its proxy opens the model backend, the deploy target (GETs by policy) and Telegram;
  it gets the QA Telethon identity only after a per-run proof, and no platform secret.
- `mega-live` proves the QA Telethon session (authorized, QA identity, reaches the stand bot) before any spend,
  and the stand hands it to qa-worker alone; paid run 36147402976 had blocked on missing credentials.
- Worker-manager retries story checkout when GitHub briefly returns repository 404, sharing scaffolder's 30 s policy;
  live checks parse console retry fields and keep the 15 s active-work bound.
- A failed story-branch fetch now exits unless Git says the remote ref is absent, avoiding an incorrect new branch.
- A QA temporary-access grant or revoke only reads its grant's target application's allocations: a revoke after
  undeploy settles as revoked with no ports or SSH; a grant, or an unreadable target, fails closed.
- After its HTTP result, a Claude CLI gets 30 s under a git-lock fence to end its turn, so its
  `total_cost_usd` reaches the ledger as `provider_reported`; past the grace it is stopped, cost unknown.

## 2026-09-24

- Every `tests/live` test runs under a `pytest-timeout` `signal` bound derived in `shared/stand_deadlines.py`,
  so a hang fails as a named pytest timeout, with cleanup, before the stand runner's backstop.
- Langgraph's service QA executor fixture now records a published start, so write-guard tests exercise QA accounting after executor creation.
- QA Run accounting now totals every started executor attempt and marks post-start errors unknown; unlimited engineering replays recheck controls.
- QA executor Runs now reserve the owner's promo budget and settle provider cost into the shared ledger; health-only QA releases its hold without spend.
- New stand suite `mega-live`: `TestFullPipeline`'s two-story lifecycle with a real developer and QA executor,
  asserting provider-reported settlement; `mega-llm`, `matrix` and `TestFullPipelineLLM` are removed.
- One contract test bans `pytest.skip` in `tests/live` outside a named environment-precondition allowlist;
  a failed scaffold or engineering phase now fails naming the phase instead of skipping.
- A failed `checkout_branch` logs and records its exit code, stderr/stdout and, when silent, the dead worker
  container's state and log tail; the spawner reads a durable creation-failure record, not "Worker disappeared".
- A failed or timed-out scaffold now fails or parks the stories waiting on it, `in_progress` included, with
  a typed `StoryFailure` and an owed owner notice, instead of leaving them "in progress" for ever.
- An `in_progress` story with no task for `supervisor.planless_story_max_minutes` (60) is parked for
  human review by the state-age watchdog, so no dead planning run reads as work continuing.
- PO reads `GET /api/stories/{id}/diagnostics` (recorded cause, scaffold error, failed runs, redacted
  Loki error lines) through `get_story_diagnostics`, and must tell the owner the cause of a stop.
- Scaffold retries `git fetch` of a just-created repository with bounded backoff (~30 s) while GitHub's git
  endpoint still answers "Repository not found" or lacks `main`; other git failures still fail at once.
- CI builds through a per-Dockerfile gha layer cache, bakes the 8 import-check images in parallel, and
  plans its docker legs and the import check from path filters, so skipped legs take no runner.
- Docker jobs wait for a 15 s lint job instead of the unit suite, which runs beside them; main runs are
  never cancelled, so every merge commit gets its service and worker release.
- The stand runs the tested release: it waits for both releases before creating machines, pulls the
  service release instead of building, and warms both pulls and uv in the background after bootstrap.
- `pull-worker-images.sh` takes `WORKER_IMAGE_SUBSET` (the stand skips the factory image), and both
  release pullers fetch a chain's images concurrently.
- The stand runner's QA-switch recreate runs the release override with `--no-build --pull never` and
  refuses a run without that override, instead of building services from the checkout.
- The stand sweep gets the suites' `API_BASE_URL`, and the runner refuses before preflight when the
  sweep's own requirements are unmet, so a green suite is no longer made red by a sweep that cannot start.
- [hotfix] Deploy cleanup removes images by ID, skips what is already gone, and reports a failure as a
  warning, so a live deploy is never red over cleanup (runs 35991711761, 35993281922).

## 2026-09-23

- Worker images are released per source hash: built once as candidates beside the suites, tested by DinD
  by digest, and after the gate a same-hash commit only gets its alias marker naming those digests.

- Every Python service image installs its `requirements.lock` before `COPY shared`/`src`, infra-service's
  Ansible collections are exact pins, and `service-image-imports` fails on any image-vs-lock drift.

- The prod overlay resets every source bind-mount, so prod and stand run the released image code only;
  shared freshness now compares those images, and `service_release.py readback` checks it on the host.

- Deploy pulls the revision's service release by digest beside the worker release and runs compose on it
  with `--no-build`, so the host builds nothing; a `revision` input redeploys a previous release (rollback).
- Deploy verifies both releases from a staged worktree into a pending set; one `Switch` step alone changes
  live host state and promotes the release records only after `up`, so a failed attempt poisons nothing.
- Main CI pushes the 10 service images by merge SHA and, after a green gate, a `service-release:<sha>` marker;
  both release chains push only on a registry 404 for the marker and validate a committed record whole.
- The PR poller merges product PRs itself (no GitHub auto-merge) after writing their `REGISTRY_*` secrets
  and rewrites them each tick a PR stays armed for auto-merge, so push-main CI never builds on stale ones.
- Resource-wait park/resume and the secret ask (the only entry to `waiting_user_secret`) owe their owner notice
  in the move's transaction and deliver it via the seam, so a Redis or recipient failure cannot lose it.
- The state-age watchdog and stage notices run in their own `story_supervision` scheduler loop, each sweep
  in its own failure boundary, since neither depends on its position in the dispatcher tick any more.
- Stage notices re-read the story just before the marker write and publish and skip a stage it has left,
  so naming the real stage no longer depends on running after every routing supervisor in the tick.
- The state-age watchdog ends a wait only via `expire-state-wait`, a compare-and-set on status and anchor,
  so a story routing moved on is skipped and logged, never parked or failed, whatever runs first.
- Every CI job and docker step is bounded, with job limits covering worst-case retries, so a hung Buildx or
  image pull fails, is retried and marks `step-timeout` instead of holding CI for up to 6 h.
- CI retries uv, Buildx and image-pull downloads and writes one `CI-INFRA-FAILURE:` marker when they are
  exhausted; the Required CI Gate repeats it but still fails. Every third-party action is SHA-pinned.
- The deploy waits up to 45 min for the dispatched SHA's CI run to publish its worker release before touching
  the host, and its file-only SSH steps retry a dropped connection up to three times, never a failed script.
- `poll_merged_prs`, `poll_ci_failures` and each story completion enter one `GitHubAppClient` per operation,
  so their GitHub calls share one HTTP pool, closed on success and error, not one pool per request.
- Owed owner-notification recovery runs on its own scheduler loop and logs every outcome per sweep, so
  its failures cannot stop dispatcher ticks and tick failures cannot delay it.
- `DEFAULT_AGENT_TYPE` is required by api, langgraph and telegram_bot and by Compose, with no `claude`
  fallback; the API drops its unused optional `TELEGRAM_BOT_TOKEN` setting.
- Owed owner notifications carry `last_attempt_at`; the API grants one delivery attempt per 60 s per record
  under the row lock, so routing and the recovery sweep may run in any order or concurrently.

## 2026-09-22

- The Claude worker image retries transient installer fetch failures and checks the downloaded script before execution, keeping its pinned version check.
- Live-test cleanup and inventory now require `API_BASE_URL`; inventory names servers skipped by cleanup policy and fails when all registered servers are skipped.
- Each scaffold operation enters one `GitHubAppClient` context for all its GitHub calls, replacing the
  scaffolder's process singleton, so its HTTP pool closes on success, failure and cancellation.
- Temporary QA access cleanup now runs on its own scheduler loop and logs each sweep, so dispatcher
  failures cannot delay cleanup and sweep failures cannot stop dispatcher ticks.
- Cleanup escalation waits for `runs.qa_routed_at`, set only by the story transition that routes the QA run,
  so the access sweep may run before or after QA routing; run writes carrying `qa_routed` metadata get 422.
- Migration `4d8e1f2a3b5c` deletes revoked target-less temporary access rows, makes both target columns NOT NULL
  and drops `env_key`/`subject`; the API loses its legacy-record rejection, list filter and legacy drain.
- The production sweep now owns only its two active title prefixes after retiring the legacy third
  production sweep prefix, whose inventory proved no residual resources remained.
- One `EmbeddingClient.generate` call now sends all of its batches through a single HTTP pool that closes
  when the call ends, instead of opening a new client for each batch.
- GitHub App operations can now share one explicitly scoped HTTP pool, while callers outside a
  lifecycle retain per-request clients and environment-contract loading uses the bounded path.
- The live-test sweep now has a fail-closed read-only inventory for each contour prefix, so the PO
  can prove retired-prefix residue is absent from production before its legacy entry is retired.
- Docs now match the code: `engineering_attempt_ledger`, not `runs`, owns token and cost accounting, and the
  env-default rule allows documented defaults only for safe presentation, logging and local ergonomics.
- Executor profile compatibility is version-bound end to end: Codex owns its serde/JWT parser and both
  adapters carry pinned provenance, so vendor upgrades cannot silently reuse a stale private-format contract.

Entries before 2026-09-22 are intentionally milestone-level. Minor fixes, CI repairs, and closely
related implementation steps are folded into the product milestone they enabled.

## 2026-09-21

- Production/live acceptance matured into a two-story, zero-intervention level-1 proof with durable run evidence,
  cleanup/residue checks, real registration, Product Brief coverage, deployment, QA, owner notification and reuse.
- Scheduler lifecycle supervision gained bounded stage waits, durable owner/admin notices, PR recovery, explicit task
  resume, worker/image cleanup and stronger first-boot stand reliability.
- Worker and QA execution became harder to contaminate: injected files stay out of product history, workspace/worker
  teardown is ownership-aware, and Telegram probes preserve bounded final-state evidence.
- Codex/Claude subscription diagnostics and host-profile parsing became versioned, locked and fail-closed instead of
  treating malformed or concurrently refreshed credentials as healthy.

## 2026-09-15

- Product Briefs became the user-facing contract for language, usage examples, limitations, initial settings and QA
  expectations; architect admission rejects or returns requirements that central QA cannot actually verify.
- Generated products gained durable initial-owner/user access flows and seeded settings, while temporary QA access,
  grant/revoke deploys and target readiness were fenced by exact run/episode evidence.
- Provisioning became identity- and episode-fenced through generated SSH keys, readiness receipts and atomic finalization,
  so operator edits, partial runs and ambiguous replays fail closed.
- QA outcomes were split into product failures versus capability/access blockers, preventing infrastructure defects from
  spawning product fix tasks and routing unrecoverable checks to explicit human review.

## 2026-09-01

- The live stand became an ephemeral, provider-owned acceptance environment with run-owned infrastructure, credential-safe
  evidence, cost accounting, cleanup proof and exact suite contracts instead of relying on static hosts.
- Paid engineering/QA work gained durable admission, budget policies, executor decisions, reservations, a ledger-derived
  balance, emergency controls and auditable refusals/retries.
- Generated-service access moved to durable typed grant intents with active readback; temporary QA capabilities and
  deployment diagnostics gained bounded recovery, redaction and stale-attempt fencing.
- Core services were decomposed around clearer boundaries: project API domains, scheduler supervision, worker-manager
  collaborators and contract registries, while dead LangGraph/provisioning compatibility layers were removed.

## 2026-08-15

- API authentication and authorization were centralized around the internal key/LK actor boundary, with shared internal
  API transport, strict DTO reuse and fail-closed Time4VPS management.
- CI/release reproducibility was tightened with uv lockfiles, pinned images/template revisions, required coverage gates,
  source-hash freshness checks and deploy-by-validated-commit semantics.
- QA gained capability-backed Telegram interaction evidence, while worker lifecycle and application undeploy became
  ownership-aware, idempotent and explicit about infrastructure blockers.
- Production observability and administration matured through strict dashboard contracts, executor diagnostics, paid-work
  controls, Loki/Grafana integration and safer operator access.

## 2026-08-01

- Temporary deployed-product access became a durable scheduler-reconciled state machine; deploys pin exact SHAs and
  analytics distinguish genuine no-traffic from failed collection.
- Generated Telegram bot identity/audience handling moved behind server-side typed contracts, with token ownership checks,
  teardown cleanup and real-user QA credentials.
- Project runtime identity split into immutable slugs versus display titles, and deploy/cleanup/allocation consistently
  adopted the slug and exact application/server ownership.
- The live/template compatibility harness became a permanent fail-closed CI surface with isolated cleanup, provenance
  artifacts and explicit release-candidate testing.

## 2026-07-18

- Cross-service contracts were tightened around typed Run results, canonical vocabularies, typed response lifecycle fields
  and typed engineering queue consumption; dead compatibility shims and unused worker lifecycle layers were removed.
- OpenAI Codex joined Claude and Factory as a first-class developer-worker backend with isolated credentials and pinned
  worker images.
- CI gained a stable Required CI Gate, broader offline live coverage, template pinning and stricter local/CI parity.
- Post-merge QA/deploy flow gained exact acceptance criteria, bounded repeated-failure handling, reliable worker teardown
  and deployment against persisted allocations and exact merged SHAs.

## 2026-07-10

- API/service tests were brought under internal-authenticated execution and the required CI gate, closing gaps where local
  or service suites could pass without exercising production authentication behavior.

## 2026-05-29

- Redis client compatibility was updated for redis-py 8 while preserving consumer semantics, and QA prompts were moved into
  the common prompt package without changing behavior.

## 2026-04-09

- Queue consumers gained terminal-message cleanup and periodic trimming of stale/orphan streams.

## 2026-03-21

- The first user dashboard shipped with LK JWT authentication, owner-scoped analytics, project KPIs/service status and a
  Telegram dashboard entry point.
- Production logging connected project-labelled container logs through Promtail/Loki for per-project visibility.

## 2026-03-07

- Planning moved from markdown into first-class DB entities: WorkItem evolved into Task/Run, with Stories, repositories,
  events, dependencies, priorities, CRUD/action APIs and generated backlog/roadmap views.
- Engineering gained PR/CI lifecycle states, deploy prechecks, seeded repository/story history and task dependency handling.
- The service architecture was simplified by removing Milestone as a parallel planning entity and standardizing project
  identifiers and repository ownership.
- Secret writes became atomic and user-aware, while PO requirements gathering, web search, environment hints and worker
  task injection established the first end-to-end product-generation workflow.

## 2026-02-28

- Worker execution stabilized around persistent project workspaces, worker reuse, compose proxying, isolated networks,
  Redis Streams with typed contracts/PEL recovery and bounded stale-worker cleanup.
- Production deploy foundations shipped: encrypted secrets, per-server SSH keys, GitHub Actions deployment, registry/TLS,
  backups and a production Compose overlay.
- Service/integration CI was expanded and parallelized, with shared contracts/constants replacing duplicated service-local
  definitions and E2E skills exercising the Line-2 pipeline.

## 2026-02-15

- The initial deploy architecture landed with Fernet-backed secret handling, environment groups, GitHub Actions deployment,
  webhook-driven releases and a self-hosted registry behind TLS.
- The PO migrated from a CLI subprocess to an async ReactAgent consumer with reminders and direct tool access.
