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
6. Save `tree` output to repository config in DB
7. Update `project.status = scaffolded`

**Outputs**: GitHub repo with full scaffolded project, workspace on disk, tree in DB

**Key property**: workspace persists on disk at `/data/workspaces/{repo_id}/`.
This same directory is mounted into worker containers later.

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
6. System auto-appends a **final task**: "Run full test suite, verify CI green, smoke test"
7. Transitions story to `in_progress`

**Outputs**: Tasks in `todo` status, linearly chained

**Rules**:
- Simple project = 1 task + auto CI task
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

**First task in story**:
1. Worker-manager creates worker container
2. Mounts workspace volume: `/data/workspaces/{repo_id}/ → /workspace`
3. Project is already scaffolded — code, venv, git all ready
4. Writes `TASK.md` into workspace (task description + acceptance criteria)

**Each task** (including first):
1. Claude Code is invoked with: `claude --task "Read TASK.md and AGENTS.md, then implement"`
2. Claude reads AGENTS.md (auto-loaded by Claude Code from project root)
3. Claude reads TASK.md (current task)
4. Claude implements, writes tests, runs them
5. Claude should smoke-test: `make up`, check logs, curl endpoints
6. Claude commits (but does not push yet — except final task)
7. Claude returns summary of what was done
8. Summary → **TaskEvent** in DB
9. Worker-manager reports task completion
10. Dispatcher transitions task to `done`

**Next task in same story**:
1. Same worker container, same workspace (workspace has state from previous tasks)
2. New `TASK.md` written with next task + previous task events as context
3. Claude invoked again — fresh process, sees accumulated codebase
4. Repeat

**Final task (auto-generated CI check)**:
1. TASK.md: "Run full test suite. Push to GitHub. Wait for CI. If CI fails, fix and retry."
2. Claude pushes, monitors CI
3. If CI green → task done → story ready for deploy
4. If CI red → Claude reads logs, fixes, pushes again
5. If Claude can't fix → task fails → supervisor creates retry task

### Worker reuse

- One worker container per story (not per task)
- Container stays alive between tasks
- Workspace volume persists state (code, venv, node_modules, etc.)
- Worker is destroyed after story completes (or fails permanently)

---

## Phase 5: Deploy

**Actor**: Deploy worker (consumes `deploy:queue`)

**Trigger**: All tasks in story `done` → dispatcher completes story → publishes deploy

1. Resolve server for the project (or provision new one)
2. Set GitHub repository secrets (DEPLOY_HOST, SSH keys, etc.)
3. Trigger GitHub Actions deploy workflow
4. Wait for deploy to complete
5. Verify service is reachable (healthcheck)

**Outputs**: Running service on server with domain + SSL

---

## Phase 6: Post-Deploy QA

**Actor**: QA agent (runs on the target server, has Playwright/Telethon MCP)

**Trigger**: Successful deploy

1. Receives full story description + acceptance criteria
2. Tests the deployed service end-to-end
3. Tries boundary cases, error scenarios
4. If everything passes → story `completed` → PO notified
5. If something fails:
   - Creates new task(s) describing the failure
   - Pipeline loops back to Phase 4 (dispatcher picks up fix task)
   - Same worker, same workspace — fix and re-deploy
   - QA runs again after next deploy
6. Loop continues until QA passes

**Outputs**: Story `completed` OR new fix tasks

---

## Phase 7: Notification

**Actor**: PO agent (via Telegram)

1. PO receives story completion event
2. Sends user a message: project is ready, here's the URL
3. If user requests changes → PO creates new story → back to Phase 3

---

## Status Flow

### Project
```
draft → scaffolding → scaffolded → developing → deployed
```

### Story
```
created → in_progress → completed
                      → failed (after max retries)
```

### Task
```
backlog (manual/standalone tasks, not in active story)
todo → in_dev → in_ci → testing → done
              → blocked (waiting on another task)
              → failed → todo (retry, up to max_iterations)
              → cancelled (sibling of failed task, or manual)
```

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
- Full story description + acceptance criteria
- Deployed service URL
- MCP tools (Playwright, Telethon, curl)
