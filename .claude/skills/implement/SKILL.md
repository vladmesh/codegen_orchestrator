---
name: implement
description: Implement the current sprint task using TDD. Reads task from sprint directory, creates git branch, updates task status on completion. Main development skill.
allowed-tools: Bash, Read, Write, Edit, Grep, Glob, Agent, Skill
argument-hint: "[task-file-path]"
---

# Implement Task

The main development skill. Implements the current sprint task (or a specific one) using TDD workflow.

## Key References
- [docs/TESTING.md](docs/TESTING.md) — test layers, service vs integration tests, compose files
- [docs/CONTRACTS.md](docs/CONTRACTS.md) — queue DTOs, shared enums (use instead of literals)

## Input

- No arguments: pick the first pending task from the current sprint phase (reads `docs/STATUS.md`)
- `<path>`: implement the specific task file (e.g. `docs/sprints/001-foo/tasks/phase0-task1-bar.md`)

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

### 1. Load task

**If task file path given:** read that file directly.

**If no argument:** read `docs/STATUS.md` to get current sprint and phase. Then find the first pending task:

```bash
SPRINT_DIR="docs/sprints/<sprint-slug>"
PHASE_NUM=<current phase number>
grep -l "Status: pending" "$SPRINT_DIR/tasks/phase${PHASE_NUM}-"*.md | head -1
```

If no pending tasks — STOP: "All tasks in current phase are done. Run `/go` to close the phase."

Read the task file. Extract: title, description, tests, acceptance criteria.

Update the task file status to `in_progress`.

### 2. Assess complexity

Read the task description and acceptance criteria. Check if the task is:
- **Simple** (single file change, clear fix, < 3 files affected): proceed directly
- **Complex** (multi-file, new feature, unclear scope): read related code first, build a mental plan

For complex tasks, also read:
- Sprint's `sprint.md` for overall context
- Related brainstorms if referenced
- The code areas mentioned in the description

### 3. Create git branch

Create and switch to a working branch:
```bash
SPRINT_NUM=<sprint number>
PHASE_NUM=<phase number>
TASK_NUM=<task number>
SLUG=<slugified task title>
BRANCH="wi/s${SPRINT_NUM}-p${PHASE_NUM}-t${TASK_NUM}-${SLUG}"
git checkout -b "$BRANCH" 2>/dev/null || git checkout "$BRANCH"
```

If already on a `wi/` branch for this task, stay on it.

### 4. TDD cycle

Follow Red -> Green -> Refactor. **Test behavior, not implementation** (see CLAUDE.md "Testing Philosophy"):

1. **Red**: Write failing test(s) based on the task's "Tests First" section. **Choose the right test level:**
   - Touches DB/Redis → service test (`services/{name}/tests/service/`)
   - Crosses service boundaries → integration test (`tests/integration/{suite}/`)
   - Pure logic only (parsers, validators) → unit test (`tests/unit/`)
   Run the test to confirm it fails.
2. **Green**: Write minimal code to make tests pass. Run the test.
3. **Refactor**: Clean up if needed. Run `make lint` and fix issues.
4. **Commit**: meaningful commit message (e.g. `feat(worker): isolate network`).

### 5. Push + PR + CI

**MANDATORY — this is a HARD GATE. Do NOT update task status to done until CI is green.**

After the last commit:

1. **Push branch**:
```bash
git push -u origin "$BRANCH"
```

2. **Create PR** targeting main:
```bash
gh pr create --title "<task title>" --body "Sprint: <sprint-slug>, Phase: <N>, Task: <M>"
```

3. **Poll CI on the PR** — every 60s, up to 15 min:
```bash
gh run list --branch "$BRANCH" --limit 1 --json status,conclusion
```

4. **CI red** — read logs via `gh run view --log-failed`:
   - Fix the issue, commit, push, re-poll.

5. **CI green** — proceed to step 6.

### 6. Testing (smoke)

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

3. **Test red** — fix, commit, push, re-poll CI (step 5.3), re-test.
4. **Test green** — proceed to step 7.

### 7. Merge + Complete

**Gate**: only enter when both CI and testing are green.

1. **Merge PR** (Claude MUST merge — do not leave PR open):
```bash
gh pr merge --squash --delete-branch
```

2. **Switch to main and pull**:
```bash
git checkout main && git pull
```

3. **Update task file status** to `done`. Fill in Developer Notes with decisions, gotchas, what changed.

4. **Check acceptance criteria** — mark each criterion as checked `[x]` in the task file.

5. **Commit** task status update on main:
```bash
git add "docs/sprints/<sprint>/tasks/<task-file>"
git commit -m "done: <task title>"
```

Do NOT push — doc-only commits stay local.

### 8. Report

> **STOP. Before writing the summary, complete the Skill Feedback step below. Do NOT skip it.**

**8a. Skill Feedback** — review the session for problems with THIS skill:

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

**8b. Print summary**:
- Task: Phase N Task M — Title
- Tests: X passed, Y added
- Files changed: list
- Next: run `/go` to continue

## Important

- If you need to change `shared/contracts/` or DB schema that wasn't in the task description — STOP and ask the user.
- Don't skip tests. Every task should have at least one test unless it's pure documentation.
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

If the task includes a live/integration test step — **it is mandatory, do not skip it.** "Unit tests already cover it" is not a valid argument — unit tests mock dependencies, live tests verify real wiring. They complement each other but do NOT substitute.

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
