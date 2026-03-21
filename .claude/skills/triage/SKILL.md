---
name: triage
description: Process E2E reports, completed brainstorms, and audit reports into backlog tasks. Routes issues by type (orchestrator/template/meta/infra). Reorders backlog Queue when roadmap changes.
allowed-tools: Bash, Read, Write, Edit, Grep, Glob
argument-hint: "[--source e2e|brainstorms|audit|all]"
---

# Triage

Process incoming reports and convert findings into backlog tasks in `docs/backlog.md`.

## Input

- `--source e2e` — only process E2E reports
- `--source brainstorms` — only process brainstorms
- `--source audit` — only process audit reports
- `--source all` or no arguments — process all sources

## Backlog Helpers

All task management is local via `docs/backlog.md`.

### Find next tag number

Parse highest existing tag from backlog and plan files:
```bash
NEXT_TAG=$(grep -ohP '#(\d+)' docs/backlog.md docs/plans/*.md 2>/dev/null | grep -oP '\d+' | sort -n | tail -1)
NEXT_TAG=$((NEXT_TAG + 1))
```

### Add a task to backlog

Append a new `### #<tag> <Title>` section to `docs/backlog.md` under `## Queue`, with Priority, Status, and Brief fields. Use the Edit tool to insert before the `## Done` section.

Priority mapping: `CRITICAL > HIGH > MEDIUM > LOW`

### Search existing tasks (dedup)

Before creating any task, search `docs/backlog.md` for keywords from the problem description. Use Grep to check for matching titles or descriptions.

## Sources

### 1. E2E Reports (`docs/e2e_results/`)

Read all `.md` files in `docs/e2e_results/`. For each report, find the `## Problems Found` section.

For each problem with structured fields:
- **Severity**: critical / major / minor / info
- **Type**: orchestrator / template / meta / infra
- **Backlog**: `#XX` (existing), `new` (create), or `—` (skip)

Only process problems where `Backlog: new` or `Backlog: template`.

- `Backlog: new` → add task to `docs/backlog.md`, update report to `Backlog: #XX`.
- `Backlog: template` → add task with `[template]` prefix in title, update to `Backlog: template (triaged)`.

After creating a task, **update the report file** to mark the problem as processed.

If a report has no structured Problems section (old format), scan for issues manually, classify them, and **rewrite the Problems section** in structured format with appropriate `Backlog` values.

### 2. Brainstorms (`docs/brainstorms/`)

Scan `docs/brainstorms/*.md` for files with `> **Status**: done` (not yet triaged).

Find the `## Action Items` section in each brainstorm. For each item:
- `→ backlog #XX` — already in backlog, skip
- `→ idea: "..."` — add to `docs/ideas.md`
- `→ new task: "..."` — add task to `docs/backlog.md`

After processing, update the brainstorm's Status to `triaged`.

### 3. Audit report (`docs/audit.md`)

If exists, read and extract actionable items not already in backlog.
Search existing tasks in `docs/backlog.md` before creating new ones.

## Routing by Type

| Type | Action |
|------|--------|
| `orchestrator` | Add task to `docs/backlog.md` |
| `template` | Add task with `[template]` prefix in title |
| `meta` | Add task with `[meta]` prefix in title |
| `infra` | Don't create tasks. Collect and list at the end for human decision. |

## Regression Detection

Before creating a new task, check the `## Done` section of `docs/backlog.md`.

Search by keywords, affected service, error pattern. If a problem matches a completed item:
1. **Reopen** — move the task from `## Done` back to `## Queue`, add `[REGRESSION]` prefix
2. **Update** — add regression context to its Brief
3. Do NOT create a duplicate task

## Deduplication

Before creating any task, search `docs/backlog.md` for matching keywords. If a matching item exists, skip (or update its Brief if new info is available).

## Roadmap ↔ Backlog Sync

After processing all sources, synchronize ROADMAP.md with backlog.

### Step 1: Ensure every ROADMAP item has a task

Parse each incomplete (`- [ ]`) item in ROADMAP.md. For each item:
- If it contains `(#XX)` — verify `#XX` exists in `docs/backlog.md`. If not found, report as orphan.
- If it has no `(#XX)` — search backlog by keywords for a matching task.
  - **Found** → update the ROADMAP line to include `(#XX)`.
  - **Not found** → report in triage output under `### ROADMAP items without backlog tasks`

### Step 2: Reorder Queue by phases

Compare ROADMAP.md and backlog.md git modification times:
```bash
git log -1 --format="%H %ai" -- docs/ROADMAP.md
git log -1 --format="%H %ai" -- docs/backlog.md
```

Reorder if ROADMAP.md was modified more recently than backlog.md, OR if new tasks were created in this triage run.

How to reorder — rearrange `### #<tag>` sections within `## Queue` in `docs/backlog.md` to match ROADMAP phase priorities:
- Tasks in earlier/active phases → move higher
- Tasks in future phases → move lower

## Commit (DO NOT push — doc-only commits stay local to avoid wasting CI minutes)

After all processing, commit changed files:

```bash
git add docs/backlog.md docs/e2e_results/ docs/brainstorms/ docs/ideas.md docs/ROADMAP.md
git commit -m "triage: <N> tasks created, <M> reports processed"
```

## Output

Print a summary table:

```
## Triage Report

### Processed
| Source | File | Problems | Tasks Created | Skipped (dup/known) |
|--------|------|----------|---------------|---------------------|
| e2e    | todo_api-20260303-levelC-3.md | 2 | 1 (#23) | 1 (known #22) |
| brainstorm | agent-hierarchy.md | 3 | 0 | 3 (already in backlog) |

### Created Tasks
- #23 Deploy timeout retry logic (HIGH)

### Needs Human Decision (infra)
- Server disk space monitoring (from E2E report)

### Brainstorms Triaged
- agent-hierarchy.md: done → triaged

### Queue Reordered (roadmap changed)
- Moved up: #27 PO tools pass user_id (Phase 2A)
- Moved down: #2 Agent Hierarchy (Phase 4)
```

If no reordering was needed, omit this section.

### Memory Review (Mandatory)

**Before generating your final response, review your memory for feedback:**
Did you have to fix any unexpected errors, correct wrong commands, or guess missing information during this task?
If yes, you **MUST** append an entry to `docs/skill-feedback.md` right now, following the format described in the **Self-Feedback** section below.

## Self-Feedback

During your final memory review, if you encountered any of the following — add an entry to `docs/skill-feedback.md`:

- A command or path in this skill was **wrong or outdated**
- A step was **missing context** that you had to figure out yourself
- A step could be **simplified or reordered** for better flow
- The skill **gave ambiguous instructions** that led to a wrong first attempt

Entry format:

```markdown
## [triage] — <today's date>
- **Type**: bug | missing-info | optimization
- **Quote**: "<exact line or section from this skill>"
- **Problem**: <what went wrong or was missing>
- **Suggested fix**: <concrete change to the skill text>
```

Only write feedback that is **specific and actionable**. Skip vague impressions.
