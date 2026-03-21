---
name: implement
description: Implement the current task using TDD. Reads plan from docs/plans/, creates git branch, updates CHANGELOG on completion. Main development skill.
allowed-tools: Bash, Read, Write, Edit, Grep, Glob, Agent, Skill
argument-hint: "[#ID]"
---

# Implement Task

The main development skill. Implements the current task (or a specific one) using TDD workflow.

## Key References
- [docs/TESTING.md](docs/TESTING.md) — test layers, service vs integration tests, compose files
- [docs/CONTRACTS.md](docs/CONTRACTS.md) — queue DTOs, shared enums (use instead of literals)

## Input

- No arguments: pick the first task from `docs/backlog.md` Queue (highest priority)
- `#ID` (e.g. `#8`): start that specific task

## Protocol

### 0. Sync git

Ensure local repo is fully synchronized with remote before starting any work:

```bash
# 1. Check for uncommitted changes — commit or stash them
git status --short
# If there are changes: commit doc-only changes, stash anything else

# 2. Pull latest main (fast-forward only to avoid surprise merges)
git checkout main
git pull --ff-only

# 3. If ff-only fails (local diverged from remote), reset to remote:
#    This happens when squash-merged PRs leave orphan local commits.
git reset --hard origin/main
```

After this step, `git status` should show clean working tree on main, up to date with origin.

### 1. Load context

Find the task in `docs/backlog.md`:

**If `#ID` given:** search for `### #<ID>` in `docs/backlog.md`. Extract title, priority, brief.

**If no argument:** take the first `### #` entry under `## Queue`.

If nothing found — STOP: "No tasks in backlog. Create one via /brainstorm + /triage."

Update the task's status in `docs/backlog.md` to `in_dev`.

### 2. Assess complexity & ensure plan

Check if a plan file exists in `docs/plans/` matching the task tag:
```bash
ls docs/plans/<tag>-*.md 2>/dev/null
```

**If plan exists** — read it. Proceed to step 3.

**If no plan** — assess complexity from the description:
- **Simple task** (single file change, clear fix, < 3 files affected): proceed without a plan, use description as guide.
- **Complex task** (multi-file, new feature, schema changes, unclear scope): auto-generate a plan by invoking /plan:

```
Use the Agent tool to run: "Run /plan for task #<tag>. The task: <title>. Description: <description>"
```

After the subagent completes, read the plan file from `docs/plans/`.

### 3. Create git branch

Create and switch to a working branch:
```bash
TAG=<tag from step 1>
SLUG=<slugified title>
BRANCH="wi/${TAG}-${SLUG}"
git checkout -b "$BRANCH" 2>/dev/null || git checkout "$BRANCH"
```

If already on a `wi/` branch for this task, stay on it.

### 4. Understand the task

Read the plan file and task description.

**Source brainstorm**: if the description or plan mentions a brainstorm, read it from `docs/brainstorms/` for additional context.

**Plan steps**: parse the steps to get:
- **Input**: what files/systems to read
- **Output**: what should change
- **Test**: what to test

If no plan exists (simple task) — use the description and your judgment.

### 5. TDD cycle (per step)

Follow Red -> Green -> Refactor. **Test behavior, not implementation** (see CLAUDE.md "Testing Philosophy"):

1. **Red**: Write failing test(s) based on the step's Test spec. **Choose the right test level:**
   - Touches DB/Redis → service test (`services/{name}/tests/service/`)
   - Crosses service boundaries → integration test (`tests/integration/{suite}/`)
   - Pure logic only (parsers, validators) → unit test (`tests/unit/`)
   Run the test to confirm it fails.
2. **Green**: Write minimal code to make tests pass. Run the test.
3. **Refactor**: Clean up if needed. Run `make lint` and fix issues.
4. **Commit**: meaningful commit message referencing the backlog item (e.g. `fix(worker): isolate network (#22)`).

### 6. Push + PR + CI

**MANDATORY — this is a HARD GATE. Do NOT touch docs (CHANGELOG, backlog) until CI is green.**

After the last step is committed:

1. **Push branch**:
```bash
git push -u origin "$BRANCH"
```

2. **Create PR** targeting main:
```bash
gh pr create --title "#$TAG — $TITLE" --body "Implements #$TAG"
```

3. **Poll CI on the PR** — every 60s, up to 15 min:
```bash
gh run list --branch "$BRANCH" --limit 1 --json status,conclusion
```

4. **CI red** — read logs via `gh run view --log-failed`:
   - Fix the issue, commit, push, re-poll.

5. **CI green** — proceed to step 7.

While CI is running or red: NO changes to CHANGELOG.md or backlog.

### 7. Testing (smoke or E2E)

**Gate**: only enter when CI is green.

1. **Rebuild and restart services** (MANDATORY before any testing):
```bash
make rebuild
```

2. **Smoke test**:
- `make up` if stack is not running
- Curl API endpoints affected by the change, verify responses
- Check Redis streams if relevant
- Review structlog output: `docker compose logs --tail=50 <service>` — look for errors
- Confirm no crashes, no unhandled exceptions

3. **Test red** — fix, commit, push, re-poll CI (step 6.3), re-test.
4. **Test green** — proceed to step 8.

### 8. Merge + Complete

**Gate**: only enter when both CI and testing are green.

1. **Merge PR** (Claude MUST merge — do not leave PR open):
```bash
gh pr merge --squash --delete-branch
```

2. **Switch to main and pull**:
```bash
git checkout main && git pull
```

3. **Update task status** in `docs/backlog.md`:
   - Remove the task's `### #<tag>` section from `## Queue`
   - Add a line to `## Done` section: `- #<tag> <title> — <today's date>`

4. **Update `docs/CHANGELOG.md`**:
- Add entry under today's date
- Use correct section: Added / Changed / Fixed / Removed
- Reference backlog item ID

5. **Commit** doc updates on main (DO NOT push — doc-only commits stay local to avoid wasting CI minutes):
```bash
git add docs/CHANGELOG.md docs/backlog.md
git commit -m "docs: complete #<ID> — <title>"
```

### 9. Report

> **STOP. Before writing the summary, complete the Skill Feedback step below. Do NOT skip it.**

**9a. Skill Feedback** — review the session for problems with THIS skill:

Did you hit any of these during this task?
- A command or path in this skill was **wrong or outdated**
- A step was **missing context** that you had to figure out yourself
- A step could be **simplified or reordered** for better flow
- The skill **gave ambiguous instructions** that led to a wrong first attempt

If yes — append an entry to `docs/skill-feedback.md` **right now**, before proceeding:

```markdown
## [implement] — <today's date>
- **Type**: bug | missing-info | optimization
- **Quote**: "<exact line or section from this skill>"
- **Problem**: <what went wrong or was missing>
- **Suggested fix**: <concrete change to the skill text>
```

If nothing went wrong — skip the file write, but you must still explicitly confirm: "Skill feedback: none."

**9b. Print summary**:
- Task: #ID — Title
- Steps completed: N/N
- Tests: X passed, Y added
- Files changed: list
- Next: suggest running `/implement` to pick next task

## Important

- If you need to change `shared/contracts/` or DB schema that wasn't in the plan — STOP and ask the user.
- Don't skip tests. Every step should have at least one test unless it's pure documentation.
- Run `make lint` before every commit.

### Code discipline (see CLAUDE.md "Critical Anti-Patterns" for full details)

- **Fail-fast**: No defaults, no fallbacks, no `get(key, default)`. Missing value = crash = fast fix. No "just in case" branches.
- **Enums & schemas only**: Use `TaskStatus.DONE`, not `"done"`. Use `EngineeringMessage(...)`, not `{"data": ...}`. If a type doesn't exist — create it in `shared/contracts/`.
- **Glossary compliance**: Worker = ephemeral Docker container with CLI agent. Consumer = queue listener role. Don't confuse them. Check [docs/GLOSSARY.md](docs/GLOSSARY.md).

### Failing tests policy

**All tests must pass before pushing.** There is no such thing as "pre-existing failures" — CI blocks merges on red tests, so main is always green. If `make test-unit` shows failures:
- They are caused by your changes (even if indirectly — e.g. you changed a contract/DTO and a test in another service still uses the old shape).
- Find and fix them.
- If a failure reveals a deep architectural problem unrelated to your task that requires serious rework — **STOP immediately**. Do NOT commit, do NOT push. Report the issue to the user with details and wait for guidance.

### Live test policy (service tests & integration tests)

If the plan includes a live/integration test step — **it is mandatory, do not skip it.** "Unit tests already cover it" is not a valid argument — unit tests mock dependencies, live tests verify real wiring. They complement each other but do NOT substitute.

Unit tests = all external services mocked. Can only **supplement** a live test, never **replace** it.

There are two types of live tests. Choose the lightest one that fits:

**Service tests** — single service + its infra deps (db, redis). Fast, **runs automatically in CI**.
- Compose files: `docker/test/service/*.yml` (e.g. `api.yml`, `langgraph.yml`)
- Run: `make test-service SERVICE=<name>`
- Use when: testing one service's behavior against real db/redis, no cross-service calls needed.

**Integration tests** — multiple services wired together. Heavier, **runs in CI only with `run-integration-tests` label** or `workflow_dispatch`.
- Compose files: `docker/test/integration/*.yml` (e.g. `backend.yml`, `template.yml`)
- Each file auto-becomes: `make test-integration-<name>`
- Test code: `tests/integration/<suite>/`
- Use when: testing cross-service flows (e.g. deploy → QA handoff, worker spawning).

**When writing live tests:**
1. Decide: service test or integration test? Prefer service test if only one service + infra is needed.
2. Check existing compose files — can you add to an existing suite?
3. If yes — add a test file to the corresponding directory.
4. If no existing suite fits — create a new compose file. It will auto-register as a make target.
5. **Run locally** after writing: `make test-service SERVICE=<name>` or `make test-integration-<suite>`.
6. For integration tests, also add the `run-integration-tests` label to the PR.
