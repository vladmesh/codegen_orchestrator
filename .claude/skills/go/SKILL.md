---
name: go
description: Sprint dispatcher — reads STATUS.md and invokes the right skill based on current sprint state. Use when user says "go", "next", "continue", or wants to proceed with sprint work.
allowed-tools: Bash, Read, Write, Edit, Grep, Glob, Agent, Skill
argument-hint: ""
---

# Go — Sprint Dispatcher

Reads `docs/STATUS.md`, determines the next action, and invokes the appropriate skill. The user says `/go`, the system decides what to do.

## Protocol

### 1. Read state

```bash
cat docs/STATUS.md
```

Parse: current sprint (name, type, phase), phase progress table.

### 2. Decision tree (first match wins)

1. **No sprint or sprint COMPLETE** → invoke `/new-sprint`
2. **Sprint exists, current phase has no task files** → invoke `/plan-phase`
3. **Phase has pending tasks** → invoke `/implement` with the first pending task file
4. **Phase has in_progress tasks** → invoke `/implement` to resume
5. **All phase tasks done, phase not COMPLETE** → invoke `/close-phase`
6. **All feature phases COMPLETE, audit not done** → invoke `/audit`
7. **Audit done, e2e not done** → invoke `/e2e-run`
8. **Audit + e2e done, fix phase not created** → triage findings and create fix phase (see "Fix Phase Creation" below)
9. **Fix phase exists with pending tasks** → invoke `/implement`
10. **Fix phase COMPLETE, docs not updated** → invoke `/update-docs`
11. **Docs updated** → invoke `/close-sprint`
12. **Blockers exist** → report blockers, wait for human input

### 3. Determine state details

To evaluate the decision tree, check:

**Sprint existence:**
```bash
# STATUS.md has "No active sprint" or a sprint name
```

**Task files for current phase:**
```bash
# Read sprint dir from STATUS.md
SPRINT_DIR="docs/sprints/<sprint-slug>"
PHASE_NUM=<current phase number>
ls "$SPRINT_DIR/tasks/phase${PHASE_NUM}-"*.md 2>/dev/null
```

**Task statuses:**
```bash
# Check Status: field in each task file
grep -l "Status: pending" "$SPRINT_DIR/tasks/phase${PHASE_NUM}-"*.md
grep -l "Status: in_progress" "$SPRINT_DIR/tasks/phase${PHASE_NUM}-"*.md
grep -l "Status: done" "$SPRINT_DIR/tasks/phase${PHASE_NUM}-"*.md
```

**Endgame state:**
Check sprint.md for markers:
- `Audit: done` / `Audit: pending`
- `E2E: done` / `E2E: pending`
- `Fix phase: COMPLETE` / `Fix phase: pending`
- `Docs: updated` / `Docs: pending`

### 4. Invoke skill

Use the Skill tool to invoke the determined skill. Pass relevant context:

- For `/implement`: pass the task file path
- For `/plan-phase`: no args needed (reads STATUS.md)
- For `/close-phase`: no args needed
- For `/audit`: no args needed
- For `/e2e-run`: no args needed
- For `/new-sprint`: no args needed
- For `/close-sprint`: no args needed
- For `/update-docs`: no args needed

### Fix Phase Creation (step 8 details)

When audit and e2e are both done, triage their findings into three buckets:

1. Read `docs/audit.md` — extract all issues
2. Read e2e report (if any problems found)
3. Categorize each finding:
   - **Quick-fix** (<5 min, clearly correct): unused imports, obvious dead code, trivial lint → create as task in fix phase
   - **Sprint-relevant** (directly in code modified this sprint, or will get worse): contract violations in changed files, bugs found by e2e → create as task in fix phase
   - **Backlog** (tech debt unrelated to sprint, nice-to-have): add to `docs/backlog.md`, NOT to fix phase

4. Create fix phase task files in `$SPRINT_DIR/tasks/` with prefix `fix-taskN-slug.md`
5. Add "Fix phase" to sprint.md and update STATUS.md:
   - Set current phase to "Fix phase"
   - Mark it as `Current` in phase progress table

If audit + e2e found zero actionable issues (nothing for fix phase) — skip fix phase entirely, mark `Fix phase: skipped` in sprint.md endgame, proceed to docs update.

Present the triage to the user before creating tasks: "Found N issues. Quick-fix: X, Sprint-relevant: Y, Backlog: Z. Proceed?"

### 5. Report

After the invoked skill completes, print:

```
## /go — Decision
- State: <what was found in STATUS.md>
- Action: <which skill was invoked and why>
- Result: <skill outcome summary>
- Next: run `/go` again to continue
```
