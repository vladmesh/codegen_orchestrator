# Pipeline V2 — Full Flow

## Overview

```
User → Telegram → PO → Project + Repo + Stories
  → Scaffolder → Architect → Dispatcher → Worker (Claude × N) → CI
  → Deploy → QA → Done → PO → User
```

Everything is sequential per project. One story at a time, one task at a time.

---

## Phase 1: Conversation & Planning (PO)

**Actor**: PO agent (LangGraph, via Telegram)

1. User describes what they want in Telegram
2. PO asks clarifying questions (modules, integrations, secrets)
3. PO creates **Project** in DB
4. PO creates **Repository** for the project (1:1 for now, model supports multi-repo)
5. PO collects secrets from user → stores encrypted on **Repository** (tied to code, not project)
6. PO sets **modules** on project config (e.g. `backend`, `tg_bot`)
7. PO presents the **Product Brief** and the user confirms it (new product work only)
8. PO creates one or more **Stories** for the project
   - Stories are ordered by priority
   - Only the first story is active — rest wait in queue
   - If user keeps chatting, PO may add more stories
9. First story triggers the pipeline

**Outputs**: Project, Repository (empty), Secrets (on repo), Product Brief, Stories

### The Product Brief is confirmed before the story exists

New product work is every story that builds something the user asked for — the
first story of a new project and every later feature alike. For all of it the
requirement becomes durable typed data before there is anything to plan, and it
is the PO that produces it:

1. `present_product_brief` opens a revision through `POST /api/product-briefs/`
   and returns the one message the user is shown — the summary, every
   must-requirement with the id the architect will dispose of it by and either
   the user's own wording or a reference to where they said it, and the typed
   initial settings. Asked again — a retry, a restart, a second PO turn — it
   returns the *stored* revision instead of composing a second interpretation:
   the project's config carries the pointer `product_brief_id` to the revision
   presented and not yet spent, and the creation idempotency key is a
   fingerprint of the document being presented, so the same presentation is the
   same revision even after the pointer is gone.
2. The user answers. On "yes", `confirm_product_brief` echoes the stored content
   to `POST /api/product-briefs/{id}/confirm`, which refuses anything but a
   byte-for-byte match. A correction is a new revision, never an edit.
3. `create_story` refuses to create new product work without a confirmed brief,
   and binds the confirmed brief to the story it created through
   `POST /api/product-briefs/{id}/story` **before** it publishes
   `ArchitectMessage`. A bind that fails publishes nothing, and the story it
   could not back is closed as `failed` rather than left in `created` — a
   `created` story with no tasks is what the scheduler's liveness sweep
   re-publishes, and it would be planned from its prose description.

Each story gets its own brief: the revision bound to a story is spent, and the
next one is a new revision of the same project.

A `fix` story on an existing project and `reopen_story` have no brief and are
unchanged — they repair what a confirmed brief already described.

---

## Phase 2: Scaffolding

**Actor**: Scaffolder service (lightweight, no Docker SDK)

**Trigger**: New repository created (or first story on unscaffolded repo)

1. Create GitHub repo (via GitHub App)
2. Clone the repository into `/data/workspaces/{repo_id}/`
3. Run Copier and `make setup`
4. Push the scaffolded code
5. Save the tree to repository configuration
6. Set the Project lifecycle to `active`

**Outputs**: GitHub repo with full scaffolded project, workspace on disk, tree in DB

**Key property**: workspace persists on disk at `/data/workspaces/{repo_id}/`.
This same directory is mounted into worker containers later.

### Ensure-Workspace Gate

For existing (ACTIVE) projects, scaffold runs in `ensure` mode before tasks dispatch:

1. `scaffold_trigger` publishes a ScaffoldMessage with `mode=ensure` for an ACTIVE project that has TODO tasks and no `workspace_ready` in its config
2. Scaffolder checks if workspace exists on disk; if missing, clones repo + runs setup
3. Sets `workspace_ready = True` in the project's config
4. Until then the admission point refuses every dispatch of that project's tasks with `workspace_not_ready` — the check is a condition of the admission decision, on the project row it locks, not a flag the dispatcher reads
5. Worker-manager GC calls `POST /repositories/{repo_id}/notify-workspace-deleted` to clear `workspace_ready` when workspace is garbage-collected

This prevents crashes when a workspace is GC'd between tasks in a story.

---

## Phase 3: Architecture

**Actor**: Architect agent (LLM, consumes `architect:queue`)

**Trigger**: Scheduler sees story on scaffolded project → publishes to `architect:queue`

1. Architect receives story ID + project ID
2. Calls `get_story` — reads story description
3. Calls `get_project_spec` — reads project config + **tree** + key spec files
4. Sees what already exists (scaffolded infra, generated code) vs what story asks for
5. Creates **1–2 tasks** for the diff (business logic only, not infra)
   - Strict linear chain: each task `blocked_by` the previous
   - Does NOT specify implementation details — worker has AGENTS.md
6. Transitions story to `in_progress` immediately on pickup (prevents supervisor from re-publishing the same story every 30s)
7. Skips stories already decomposed (IN_PROGRESS + has tasks)

**When the story is backed by a confirmed Product Brief**, steps 1–5 happen
inside a claimed planning attempt:

- the consumer claims the attempt before invoking the agent, and plans nothing
  when the claim reports `in_progress` (another architect owns it) — see
  `docs/CONTRACTS.md`, "The Product Brief coverage-to-dispatch boundary";
- the claim is heartbeated while the agent runs, and the beat stops however the
  run ends;
- every task is created under the attempt, so it is `dispatch_admitted=false`
  until the plan is released;
- the agent records one disposition per must-requirement — the task that covers
  it, or the reason it is returned — with `record_requirement_coverage`;
- the consumer then calls `admit` once. `incomplete` releases nothing and is the
  result of the job, whatever the agent said about its own run.

The same brief may also carry the typed settings the product starts life with
(`initial_settings`). They reach the agent on the graph state and by name in the
instructions, and they are **not** disposed of one by one: the platform writes
their values after deploy (Phase 5). What the plan owes them is the declaration
that makes them writable — each key in the generated product's own
`services/<service>/manifest.yaml` `settings_schema`, with a schema the
confirmed value satisfies, read where the product uses it.

A must-requirement that implies a deferred or scheduled behaviour is planned the
same way, because the generated product's core schedules nothing: the plan
declares the behaviour by name in that same `manifest.yaml` under `jobs_schema`
(arguments schema `type: object`, `additionalProperties: false` — an undeclared
name is `404`), plans the module that declares `provides: ["jobs.fire"]` and
does the work on `job_fired`, and authors the checklist line QA fires from,
`- FIRE JOB <name> [WITH {json}] THEN <observable>` (step 7a of Phase 6), with
the declared name character for character. Where typed settings configure the
behaviour the observable is read off those confirmed values, and it asserts a
capability rather than a sample, so a quiet week is not a false red. A story
with no such behaviour gets no line and no declaration.

**Outputs**: Tasks in `todo` status, linearly chained

**Rules**:
- Simple project = 1 task
- Never create tasks for Docker, compose, CI, deployment — scaffolding handles it
- Focus on what the worker needs to BUILD, not how

---

## Phase 4: Execution (Dispatcher + Worker)

### Dispatcher

**Actor**: Scheduler (30s poll loop)

1. Finds `todo` tasks — candidate selection, and the whole of what this loop decides
2. Asks the admission point once per task: `POST /api/work-admission/engineering-dispatches`
3. Acts on the typed answer: publish to `engineering:queue` when admitted, execute the named repair, or record the refusal
4. Transitions task to `in_dev`

**Admission happens here, and only here.** `dispatch_todo_tasks` in
`services/scheduler/src/tasks/task_dispatcher.py` holds no dispatch condition of
its own. Whether this task may be bought a worker — the dispatchable status, the
Product Brief coverage boundary, the internal project, the blocker, the project
scaffold and workspace, the one-task-per-story fence, the prior attempt, and
finally the budget and slot — is one question answered server-side, on rows
locked in the deciding transaction, by
`services/api/src/engineering_dispatch_admission.py`. The one-at-a-time story
guard used to be a client-side check in this loop; it is now the `story_busy` and
`story_waiting_human_review` conditions of that decision, which also observe the
siblings' live engineering *runs* and not only their statuses. An admitted
decision has already created the queued Run and taken its budget hold, so what
this loop still owes is the message and the transition out of `todo`. The
operator route `POST /api/tasks/{id}/spawn-worker` enters at the same point.

The Product Brief condition is the one that can hold a whole story's plan back:
a task created under an active architect planning attempt is
`dispatch_admitted=false` and is refused with `product_brief_not_admitted` until
`POST /api/product-briefs/{id}/admit` releases the plan as a whole. Phase 3 is
the producer: a story backed by a confirmed brief is planned under a claimed
attempt and released only when every must-requirement has a disposition. A story
with no brief still creates dispatch-admitted tasks, and for those this condition
refuses nothing.

### Worker

**Actor**: Worker-manager (container lifecycle) + Claude Code (implementation)

Workers operate on **story-level feature branches** (`story/{story_id}`). Branch name flows through the full pipeline: task dispatcher → engineering consumer → developer node → worker spawner → worker-manager → worker-wrapper.

**First task in story**:
1. Worker-manager creates worker container
2. Mounts workspace volume: `/data/workspaces/{repo_id}/ → /workspace`
3. Worker-manager creates/checks out `story/{story_id}` branch in the workspace
4. Project is already scaffolded — code, venv, git all ready
5. Writes `TASK.md` into `/workspace/TASK.md` (task description + acceptance criteria)

**Each task** (including first):
1. Claude Code is invoked with a one-line redirect: `claude -p "Read TASK.md"` (full task stays in file)
2. Claude reads TASK.md (current task) and AGENTS.md (auto-loaded from project root)
3. Claude implements, writes tests, runs them
4. Claude should smoke-test: `make up`, check logs, curl endpoints
5. Claude commits and pushes to `story/{story_id}` branch
6. Claude returns summary of what was done
7. After task, wrapper archives TASK.md + REPORT.md into `.story/old_tasks/{task_id}.md`
8. Summary → **TaskEvent** in DB
9. Worker-manager reports task completion
10. Dispatcher transitions task to `done`

**Next task in same story**:
1. Same worker container and workspace
2. New `TASK.md` written with the next task and task events as context
3. Claude invoked again — `--resume` session (fresh on first task or retry)
5. Repeat

### Developer Gave-Up Escalation

If the developer agent encounters an unsolvable problem:

1. Agent calls `curl -X POST localhost:9090/result -d '{"success":false,"reason":"description"}'`
2. Worker-wrapper HTTP server validates and publishes result to Redis (status `"blocked"`, `gave_up_reason`)
3. Developer node returns `engineering_status=EngineeringStatus.GAVE_UP`
4. Engineering consumer calls `handle_worker_gave_up()`:
   - Task → `waiting_human_review` with `failure_metadata = {reason: "..."}`
   - Story → `waiting_human_review`
   - Admin notified via Telegram (warning level)
   - User notified via PO ("story_blocked" event)
   - Worker container **NOT** destroyed (admin may inspect)
5. Task dispatcher skips WHR tasks (not stuck, deliberately paused)
6. Admin calls `POST /tasks/{id}/resume` with guidance → task back to `in_dev`

### Worker reuse

- One worker container per story (not per task)
- Container stays alive between tasks
- Workspace volume persists state (code, venv, node_modules, etc.)
- Worker is destroyed after story completes (or fails permanently)
- On gave-up: worker is kept alive for admin inspection

---

## Phase 4b: PR-Based CI Gate

**Actor**: Task Dispatcher (scheduler) + PR Poller (scheduler)

**Trigger**: All tasks in story `done`

1. Task Dispatcher creates PR from `story/{story_id}` → `main`
2. Enables auto-merge (merge commit — preserves individual commits)
3. Transitions story to `pr_review`
4. Cleans up worker container (no longer needed)
5. Triggers next queued story for this project (doesn't wait for PR merge)

**CI runs on the PR:**
- **Green CI** → auto-merge → PR poller detects merged PR → deploy
- **Red CI** → PR poller detects CI failure → creates fix task → one `retry-after-ci-failure` call walks the story `failed → reopened → in_progress` server-side

**PR merge detection**: PR poller (`scheduler/src/tasks/pr_poller.py`) polls GitHub for merged PRs and CI failures on stories in `pr_review` status every 30 seconds.

---

## Phase 5: Deploy

**Actor**: Deploy worker (consumes `deploy:queue`) — pure technical worker

**Trigger**: PR merged to main (detected by PR poller) OR PO manual trigger OR Admin API

0. (Before the deploy Run exists) The producer waits for the merged commit's
   images — see below. No Run is created until they are published.
1. Resolve server for the project (or provision new one)
2. Read the registry once for the images this deploy resolved
3. Set GitHub repository secrets (DEPLOY_HOST, SSH keys, etc.)
4. Trigger GitHub Actions deploy workflow, pinned to the built commit
5. Wait for deploy to complete
6. Smoke test: HTTP `/health` for backends, Bot API `getMe` + running `tg_bot` container for bots
7. Resolve failures deterministically: typed environment failures keep their specific outcome;
   unclassified subgraph and smoke failures become RETRY
8. Seed the confirmed Product Brief's `initial_settings` into the deployed product
   (brief-backed stories only) — see below
9. Write `DeployOutcome` to `run.result`, naming the image references it deployed
10. Deploy worker does NOT transition stories or create tasks — it is a pure technical worker

**Two commits, never one**: `head_sha` is the story's commit, the pull request
head, and it stays the key for story evidence, the access grants and the
redundant-deploy check. `deployed_commit_sha` is the built commit on `main` —
`merged_pr["merge_commit_sha"]` — and it is what the images are tagged with and
what the checkout is pinned to. GitHub offers no fast-forward merge and squash
and rebase rewrite the commit, so `main`'s new head is never the PR head; asking
for the PR head's image tag asks for an image that can never exist. Both travel
on `DeployMessage` and in the deploy Run's metadata.

**Deploying a commit means deploying its images**: the environment contract's
`*_IMAGE` entries resolve to `<registry>/<owner>/<repo>-<service>:sha-<short sha>`
of the *built* commit — the tag the generated project's CI publishes through
`docker/metadata-action`'s `type=sha` — never a mutable `:latest`.

**The wait sits ahead of the deploy Run.** `poll_merged_prs` will not create a
Run until the project's own `ci.yml` run for the built commit on `main` has
concluded successfully; that run's `build-and-push` is the publication. It waits
up to 15 minutes from the merge, measured from GitHub's own `merged_at` so no
state of ours can restart it, and the story simply stays in `pr_review` between
ticks. A finished run that did not publish refuses immediately. Nothing here
builds, retriggers or repairs the project's CI. A refusal creates no Run, so it
is recorded on the story — `quarantine_reason` carrying
`deploy_outcome: images_not_published`, both commits and the CI run it read —
and the story goes to `waiting_human_review`. This is why
`DEPLOY_TIMEOUT_SECONDS` (600 s) and the live suite's `DEPLOY_TIMEOUT` (420 s)
still mean "deploy.yml + smoke", while the suite's wait for the Run to *appear*
(`DEPLOY_RUN_TIMEOUT`) is the one that spans the project's CI.

Inside the deploy, before any external effect, the deployer reads the registry
once for exactly the references it resolved. Absent images are
`DeployOutcome.IMAGES_NOT_PUBLISHED`; a registry that cannot be read at all is
`DeployOutcome.IMAGE_REGISTRY_UNREADABLE`, kept apart because "not asked" is not
an answer about the project. Both are redeployed under the ordinary retry bound.
A successful deploy names the references, their digests and the built commit in
`DeployRunResult.deployment_result` and in the service-deployment record, beside
the story commit in `deployed_sha`.

**Seeding the confirmed settings**: after health checks, the handler reads the
brief bound to this story and writes each `initial_settings` entry through the
product's own `POST /settings/set`, proving each one with `POST /settings/get`.
The `SETTINGS_WRITE_CAPABILITY` comes only from this deploy's in-memory
`secret_values` and travels as a header — never a URL, log, event or persisted
diagnostic. Every setting's disposition is recorded in
`DeployRunResult.settings_seed`. A transport failure or an unproved readback
holds the deploy back as `SETTINGS_SEED_FAILED` — its own outcome and its own
bounded supervisor route, because the owner-grant outcome is reconciled to
SUCCESS once that grant is applied and would launder the failure away; an
undeclared key, a schema-refused value, or a pinned product whose contract
predates the capability is reported beside the successful deploy, because
redeploying the same artifact would answer identically. A story with no brief,
or a brief with no settings, seeds nothing. See `docs/CONTRACTS.md`, "A brief
carries typed initial settings, and never a secret".

**Supervisor routing** (`supervise_deploying_stories()` in scheduler, 30s poll):
- Reads deploy run outcome from DB
- SUCCESS → story `testing`, create QA run, publish `QAMessage` to `qa:queue`
- CODE_FIX / SMOKE_FAILURE → create a fix task and dispatch it to `engineering:queue`
- RETRY / IMAGES_NOT_PUBLISHED / IMAGE_REGISTRY_UNREADABLE → redeploy with counter
  (max 3 consecutive failures)
- SETTINGS_SEED_FAILED → redeploy the same commit under that same counter, never
  reconciled to SUCCESS by an applied owner grant
- GIVE_UP → story `failed`, admin notified

**Deploy deduplication**: Atomic Redis `SET NX` lock per project prevents duplicate deploys.

**Lifecycle operations**: `stop` and `undeploy` actions (from Admin API, or from a PO teardown) are handled by `deploy_lifecycle` module — SSHes to server and runs `docker compose stop/down` directly, skipping the full DevOps subgraph. The message names its target application (`DeployMessage.application_id`) and the consumer acts on that one: a project can have applications on several servers, and allocation would answer with one of its own choosing.

**Outputs**: Running service on server with domain + SSL, or `DeployOutcome` in run.result for supervisor

---

## Phase 6: Post-Deploy QA

**Actor**: QA consumer (`qa-worker` container, consumes `qa:queue`) — pure technical worker

**Trigger**: Supervisor detects successful deploy → creates QA run → publishes `QAMessage`

**How it works**:
1. QA consumer receives `QAMessage` with `project_id`, `deployed_url`, `application_id`, `run_id`, optional `story_id` and `bot_username`
2. Criteria that only state GET expectations are decided directly over HTTP — no agent, no LLM
3. Otherwise the run is exploratory. The run is never performed as `servers.ssh_user` — that is the administrative account the fleet key opens, the same on a stand row as on a production one — and a target whose QA account would be `root` or that administrative account is refused here: QA does not run privileged
4. The consumer creates an isolated central workspace, writes a durable `qa_ssh_grant` record on the QA run, then mints a one-shot SSH identity on the target (`restrict`, `expiry-time`), installed and removed with the fleet key by the runner
5. The run's capability set is resolved from deployment data: physical root of the deployment directory (`readlink -f` on the target), containers of this compose project (`docker ps --filter label=com.docker.compose.project=...`), the application's allocated ports, the public URL
6. A central Codex QA worker runs from an intentionally empty ephemeral non-Git workspace, prompted with the acceptance criteria and deployed URL. It reads its injected `AGENTS.md` and `TASK.md`, uses Codex's native `--skip-git-repo-check` mode, and reaches the deployment only through the typed capability endpoint. The endpoint bounds every call to one element of the set: public GET, loopback GET on an allocated port, a read contained in the physical root, read-only docker sub-commands against a container of this deployment, container logs/inspect, Telegram probe. The target receives no Codex profile, credential or API key.
7. Telegram bots are tested by the runtime, which sends the agent's message as the QA account and returns the replies. The agent never holds the session
7a. A checklist line of the form `- FIRE JOB <name> [WITH {json}] THEN <observable>` names a scheduled behaviour of the product. The runner parses those lines itself, before any executor exists, resolves this deployment's `JOBS_FIRE_CAPABILITY` from the project's own encrypted secrets on the management host, and offers two extra calls bound to that closed set of names: `fire_job(name)` invokes the behaviour through the product's released `POST /jobs/fire`, and `job_evidence(name)` reads the record back through `POST /jobs/evidence`. The executor supplies the name and nothing else — the arguments come off the criterion, the command identity is `qa-<qa run id>-<name>` and the provenance is this QA run, so a retry of the call re-reads one execution rather than causing a second. The capability travels as the `X-Jobs-Capability` header from the management host and never enters the executor container, its environment, the `qa` CLI's arguments, the trace or a verdict. A `dispatched` command records only that the product's core published `job_fired`; the prompt, the answer and the run's facts all say that this is not evidence the behaviour ran, and the check passes on the observable the criterion states
7b. For a brief-backed story the run is also given the confirmed `initial_settings` — key, scope and value — read through `GET /api/product-briefs/by-story/{story_id}`. An acceptance step about a configured behaviour asserts against those typed values instead of reconstructing them from the story's prose. A story with no confirmed brief adds nothing and the run is exactly as it was
8. The agent submits a structured terminal verdict through the capability endpoint.
9. Workspace and target grant are destroyed on every path out, including a failed or interrupted run; anything that survives is reported as a `qa_cleanup_failed` blocker. A grant the run could not settle stays on the record for the `qa-worker` sweep
10. Write `QAOutcome` to `run.result` (PASSED / FAILED / EXHAUSTED / ERROR)
11. QA consumer does NOT transition stories or create tasks — it is a pure technical worker

**Supervisor routing** (`supervise_testing_stories()` in scheduler, 30s poll):
- Reads QA run outcome from DB
- PASSED → story `completed`, user notified via PO
- FAILED → create fix task, dispatch to `engineering:queue`, story → `in_progress`
- EXHAUSTED → story `failed` (max QA→Engineering loops reached)
- ERROR → story `failed`

**Inflight deduplication**: Uses `application_id` for dedup when no story (standalone E2E triggers). Story-based runs use `story_id`.

**Target prerequisites**: none beyond a reachable SSH account and a running deployment. QA installs
nothing on the target and needs no coding-agent CLI, LLM credentials or Telethon session there.

**QA runtime prerequisites** (orchestrator `.env`):
- `QA_EXECUTOR_AGENT_TYPE` — optional override, `codex` by default and `claude` supported explicitly.
  Compose passes it to `api` and `qa-worker`, but only the API's copy decides: the resolver reads it
  at paid-run admission and persists the choice on the Run, and `qa-worker` obeys that record. A
  change therefore takes effect only once the `api` container has been recreated with it.
- `TELETHON_API_ID` / `TELETHON_API_HASH` / `TELETHON_SESSION` — only for projects with a bot

**Outputs**: `QAOutcome` in run.result for supervisor

---

## Phase 7: Notification

**Actor**: PO agent (via Telegram)

1. PO receives story completion event
2. Sends user a message: project is ready, here's the URL
3. If user requests changes → PO creates new story → back to Phase 3

---

## Status Flow

### Project (Lifecycle)
```
draft → active → paused → archived
```
Project status is now lifecycle-only. Process states (scaffolding, developing) are derived from child entities.
Runtime state is tracked by `Application.status` (`not_deployed → running → degraded / down / stopped`).

### Story
```
created → in_progress → pr_review → deploying → testing → completed
                      → waiting_human_review → in_progress (admin resolves)
                                             → failed
                      → failed (after max retries)
         pr_review → failed → reopened → in_progress (CI failed on story branch → fix task created; one composite move)
                   → deploying (PR poller detects a merged PR)
                   → failed
         deploying → testing (deploy success → QA handoff)
                  → in_progress (deploy failure → fix task)
                  → failed
         testing → completed (QA passed)
                → in_progress (QA failed → fix task created, max 2 QA loops)
                → failed (after max QA loops)
         completed → reopened → in_progress
         failed → reopened
```
`pr_review` — all tasks done, PR created from story branch to main. Waiting for CI + auto-merge.
`deploying` is a deploy gate — story waits for successful deploy before QA.
`testing` — deployed service being tested by the QA consumer through a central ephemeral QA worker
on the management host (Codex by default, with Claude Code as an explicit `QA_EXECUTOR_AGENT_TYPE=claude` override).
`waiting_human_review` — developer reported a blocker; pipeline is paused until admin resolves.

**Who moves a Story.** Only the API does, in two places: `_do_transition`
(`services/api/src/routers/_story_helpers.py`) for a single hop, and
`_apply_chain` (`routers/_story_actions.py`) for a composite declared in
`COMPOSITE_CHAINS`. Both take the row with `SELECT ... FOR UPDATE` and check
every hop against `VALID_TRANSITIONS` before writing any of them. The one
composite today is `retry-after-ci-failure` — `failed → reopened → in_progress`,
called by `pr_poller` as a single `POST /api/stories/{id}/retry-after-ci-failure`
once it has recorded the failed CI run and created the fix task. It was three
client calls (`fail` → `reopen` → `start`), and a crash between two of them
parked the story in an intermediate status with nobody to finish it. Pollers,
supervisors and consumers now report the event that happened; none of them
sequences a Story through more than one status.

**What a story is waiting for.** Every landing status also implies a
`waiting_on` value, written by the same transition on the same locked row from
`WAITING_ON_BY_STATUS` (`shared/contracts/dto/story.py`) — never patched by a
client, and never derived by a reader:

| Story status | `waiting_on` | What has to happen |
|---|---|---|
| `created`, `in_progress`, `reopened` | `none` | the pipeline itself is working |
| `pr_review` | `ci` | CI on the story branch, then auto-merge |
| `deploying` | `deploy` | the deploy run |
| `testing` | `qa` | the QA verdict |
| `waiting_human_review` | `human_review` | an admin |
| `waiting_user_secret` | `user_secret` | the owner supplies a secret |
| `completed`, `failed`, `archived` | `none` | nothing; the story has ended |

`StoryWaitingOn.RESOURCES` is declared in the enum and no status maps to it: a
capacity wait parks the *Task* in `waiting_resources` while the Story stays
`in_progress`, so nothing sets it today. Parked stories are listed, most
recently touched first and capped, in the `waiting_stories` section of
`GET /api/admin/overview`.

### Task
```
backlog (manual/standalone tasks, not in active story)
todo → in_dev → in_ci → testing → done
              → blocked (waiting on another task)
              → waiting_human_review → in_dev (admin resumes with guidance)
                                     → backlog (admin re-queues)
                                     → failed / cancelled
              → failed → todo (retry, up to max_iterations)
              → cancelled (sibling of failed task, or manual)
```
`waiting_human_review` — developer hit an unsolvable blocker (missing credentials, contradictory requirements, broken external dependencies). Admin must provide guidance via `POST /tasks/{id}/resume` or re-queue to backlog.

---

## Sequencing Rules

1. **One story at a time** per project. Next story starts only after current completes.
2. **One task at a time** per story. The admission point enforces this, as the `story_busy` refusal; the dispatcher only asks.
3. **Tasks form a linear chain**. Each `blocked_by` the preceding task. No parallelism.
4. **Final task is auto-generated**. Always: test + CI green + push.
5. **QA loops** until pass. Each failure → new fix task → re-deploy → re-QA.

---

## Key Data Flows

### What Architect sees
- Story description (from user, via PO)
- The confirmed Product Brief backing the story, when there is one: its
  must-requirements and its typed `initial_settings`
- Project spec (modules, description, detailed_spec)
- Repository tree (from scaffolder, stored in DB)
- Key spec files (models.yaml, events.yaml if backend module)
- Existing tasks (to avoid duplicates)

### What Worker sees
- TASK.md (per-task, written by dispatcher/developer node)
- AGENTS.md (in project root, from scaffold, auto-loaded by Claude Code)
- Task events appended to `TASK.md` as context
- Full scaffolded codebase (via workspace volume)

### What QA sees
- Full story description (used to build QA prompt)
- Deployed service URL
- A capability set resolved from this deployment: physical root, its containers, its allocated ports, its URL
- A typed tool set whose every boundary comes from that set, and nothing else
- Bot username (if Telegram bot project — enables the Telegram probe tool)
