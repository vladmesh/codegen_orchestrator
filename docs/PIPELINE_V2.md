# Pipeline V2 — Full Flow

> Target architecture. Not everything works this way yet — this is the spec we're building toward.

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
2. Clone empty repo into workspace: `/data/workspaces/{repo_id}/`
3. Run `copier copy service-template workspace --data modules=... --vcs-ref=HEAD`
4. Run `make setup` (uv venv, framework generate, ruff format)
5. `git push` — scaffolded code is now in GitHub
6. Set branch protection on `main` (require PR, require `ci` status check). Non-fatal — scaffold succeeds even if protection fails.
7. Save `tree` output to repository config in DB
8. Update `project.status = scaffolded`

**Outputs**: GitHub repo with full scaffolded project, workspace on disk, tree in DB

**Key property**: workspace persists on disk at `/data/workspaces/{repo_id}/`.
This same directory is mounted into worker containers later.

### Ensure-Workspace Gate

For existing (ACTIVE) projects, scaffold runs in `ensure` mode before tasks dispatch:

1. Task dispatcher checks `repository.workspace_ready` flag before dispatching
2. If not ready, scaffold_trigger publishes ScaffoldMessage with `mode=ensure`
3. Scaffolder checks if workspace exists on disk; if missing, clones repo + runs setup
4. Sets `workspace_ready = True` on the repository
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

**Outputs**: Tasks in `todo` status, linearly chained

**Rules**:
- Simple project = 1 task
- Never create tasks for Docker, compose, CI, deployment — scaffolding handles it
- Focus on what the worker needs to BUILD, not how

---

## Phase 4: Execution (Dispatcher + Worker)

### Dispatcher

**Actor**: Scheduler (30s poll loop)

1. Finds `todo` tasks with no unresolved blocker
2. Guard: if any task in the story is `in_dev` → skip (max 1 at a time)
3. Publishes to `engineering:queue`
4. Transitions task to `in_dev`

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
1. Same worker container, same workspace (workspace has state from previous tasks)
2. New `TASK.md` written with next task + previous task events as context
3. Claude invoked again — `--resume` session (fresh on first task or retry)
4. Previous tasks visible via `.story/old_tasks/` directory
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
- **Red CI** → PR poller detects CI failure → creates fix task → story back to `in_progress`

**PR merge detection**: PR poller (`scheduler/src/tasks/pr_poller.py`) polls GitHub for merged PRs and CI failures on stories in `pr_review` status (every 30s). No webhook dependency — works reliably for all repos including newly scaffolded ones.

---

## Phase 5: Deploy

**Actor**: Deploy worker (consumes `deploy:queue`) — pure technical worker

**Trigger**: PR merged to main (detected by PR poller) OR PO manual trigger OR Admin API

1. Resolve server for the project (or provision new one)
2. Set GitHub repository secrets (DEPLOY_HOST, SSH keys, etc.)
3. Trigger GitHub Actions deploy workflow
4. Wait for deploy to complete
5. Smoke test: HTTP `/health` for backends, Telethon `/start` for tg_bot
6. Resolve failures deterministically: typed environment failures keep their specific outcome;
   unclassified subgraph and smoke failures become RETRY
7. Write `DeployOutcome` to `run.result`
8. Deploy worker does NOT transition stories or create tasks — it is a pure technical worker

**Supervisor routing** (`supervise_deploying_stories()` in scheduler, 30s poll):
- Reads deploy run outcome from DB
- SUCCESS → story `testing`, create QA run, publish `QAMessage` to `qa:queue`
- CODE_FIX / SMOKE_FAILURE → create fix task, dispatch to `engineering:queue` (legacy outcomes only)
- RETRY → redeploy with counter (max 3 consecutive failures)
- GIVE_UP → story `failed`, admin notified

**Deploy deduplication**: Atomic Redis `SET NX` lock per project prevents duplicate deploys.

**Lifecycle operations**: `stop` and `undeploy` actions (from Admin API) are handled by `deploy_lifecycle` module — SSHes to server and runs `docker compose stop/down` directly, skipping the full DevOps subgraph.

**Outputs**: Running service on server with domain + SSL, or `DeployOutcome` in run.result for supervisor

---

## Phase 6: Post-Deploy QA

**Actor**: QA consumer (`qa-worker` container, consumes `qa:queue`) — pure technical worker

**Trigger**: Supervisor detects successful deploy → creates QA run → publishes `QAMessage`

**How it works**:
1. QA consumer receives `QAMessage` with `project_id`, `deployed_url`, `application_id`, `run_id`, optional `story_id` and `bot_username`
2. SSHes to the target prod server as root (via SSH key from DB)
3. `cd /opt/services/{project_name}` — enters the deployed project directory
4. Runs `claude -p "<QA prompt>" --output-format json --max-turns 50 --model claude-sonnet-4-6`
5. QA prompt is built from story description + deployed URL. Claude tests every feature described in the story: curls endpoints, checks responses, tests edge cases. For Telegram bots, uses Telethon (pre-installed in `/opt/qa-runner/venv`)
6. Claude returns JSON: `{"pass": bool, "checks": [...], "summary": "..."}`
7. Write `QAOutcome` to `run.result` (PASSED / FAILED / EXHAUSTED / ERROR)
8. QA consumer does NOT transition stories or create tasks — it is a pure technical worker

**Supervisor routing** (`supervise_testing_stories()` in scheduler, 30s poll):
- Reads QA run outcome from DB
- PASSED → story `completed`, user notified via PO
- FAILED → create fix task, dispatch to `engineering:queue`, story → `in_progress`
- EXHAUSTED → story `failed` (max QA→Engineering loops reached)
- ERROR → story `failed`

**Inflight deduplication**: Uses `application_id` for dedup when no story (standalone E2E triggers). Story-based runs use `story_id`.

**Server prerequisites** (provisioned by `qa_runner` Ansible role):
- Claude Code CLI (standalone binary via `curl install.sh | bash`)
- `.credentials.json` OAuth session (copied from orchestrator host)
- 2GB swap (Claude Code binary extraction needs ~2GB)
- Python venv at `/opt/qa-runner/venv` with `telethon` + `httpx`
- Optional: `telethon.session` file for Telegram bot testing

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
         pr_review → in_progress (CI failed on story branch → fix task created)
                   → deploying (PR merged → webhook/polling triggers deploy)
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
`testing` — deployed service being tested by QA consumer (Claude Code on prod server).
`waiting_human_review` — developer reported a blocker; pipeline is paused until admin resolves.

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
2. **One task at a time** per story. Dispatcher guard enforces this.
3. **Tasks form a linear chain**. Each blocked_by the previous. No parallelism.
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
- Previous task events (appended to TASK.md as context)
- Full scaffolded codebase (via workspace volume)

### What QA sees
- Full story description (used to build QA prompt)
- Deployed service URL
- Claude Code CLI on the server (runs as root, cd to `/opt/services/{project}`)
- Pre-installed tools: `curl`, Telethon (in `/opt/qa-runner/venv`), httpx
- Bot username (if Telegram bot project — enables Telethon testing)
