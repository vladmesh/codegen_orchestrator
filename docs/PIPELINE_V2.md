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
7. PO creates one or more **Stories** for the project
   - Stories are ordered by priority
   - Only the first story is active — rest wait in queue
   - If user keeps chatting, PO may add more stories
8. First story triggers the pipeline

**Outputs**: Project, Repository (empty), Secrets (on repo), Stories

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

1. Resolve server for the project (or provision new one)
2. Set GitHub repository secrets (DEPLOY_HOST, SSH keys, etc.)
3. Trigger GitHub Actions deploy workflow
4. Wait for deploy to complete
5. Smoke test: HTTP `/health` for backends, Bot API `getMe` + running `tg_bot` container for bots
6. Resolve failures deterministically: typed environment failures keep their specific outcome;
   unclassified subgraph and smoke failures become RETRY
7. Write `DeployOutcome` to `run.result`
8. Deploy worker does NOT transition stories or create tasks — it is a pure technical worker

**Supervisor routing** (`supervise_deploying_stories()` in scheduler, 30s poll):
- Reads deploy run outcome from DB
- SUCCESS → story `testing`, create QA run, publish `QAMessage` to `qa:queue`
- CODE_FIX / SMOKE_FAILURE → create a fix task and dispatch it to `engineering:queue`
- RETRY → redeploy with counter (max 3 consecutive failures)
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
3. Otherwise the run is exploratory. A target whose `ssh_user` is `root` is refused here — QA does not run privileged
4. The consumer creates an isolated central workspace, writes a durable `qa_ssh_grant` record on the QA run, then mints a one-shot SSH identity on the target (`restrict`, `expiry-time`), installed and removed with the fleet key by the runner
5. The run's capability set is resolved from deployment data: physical root of the deployment directory (`readlink -f` on the target), containers of this compose project (`docker ps --filter label=com.docker.compose.project=...`), the application's allocated ports, the public URL
6. A central Codex QA worker runs from an intentionally empty ephemeral non-Git workspace, prompted with the acceptance criteria and deployed URL. It reads its injected `AGENTS.md` and `TASK.md`, uses Codex's native `--skip-git-repo-check` mode, and reaches the deployment only through the typed capability endpoint. The endpoint bounds every call to one element of the set: public GET, loopback GET on an allocated port, a read contained in the physical root, read-only docker sub-commands against a container of this deployment, container logs/inspect, Telegram probe. The target receives no Codex profile, credential or API key.
7. Telegram bots are tested by the runtime, which sends the agent's message as the QA account and returns the replies. The agent never holds the session
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

**QA runtime prerequisites** (orchestrator `.env`, read by `qa-worker`):
- `QA_EXECUTOR_AGENT_TYPE` — optional override, `codex` by default and `claude` supported explicitly
- `QA_LLM_MODEL` / `QA_LLM_BASE_URL` / `QA_LLM_API_KEY` — optional API fallback, read only after the assigned subscription executor is unavailable
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
