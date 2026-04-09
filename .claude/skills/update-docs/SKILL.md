---
name: update-docs
description: >
  Update living documentation to match current codebase state. Three modes:
  `/update-docs` — incremental, all groups, checks CHANGELOG + git log since last run;
  `/update-docs <group>` — incremental but scoped to one group;
  `/update-docs full <group>` — deep-reads actual code and compares with docs in a group.
  Groups: architecture, contracts, agents, ops, workflow.
  Use when user says "update docs", "docs are stale", "sync documentation",
  or after a batch of changes lands. Also useful from /checkpoint.
allowed-tools: Bash, Read, Write, Edit, Grep, Glob, Agent
argument-hint: "[full] <group>"
---

# Update Documentation

Keep living docs in sync with the codebase.

## Key References
- See **Documentation Map** in [CLAUDE.md](CLAUDE.md) for the full list of docs grouped by purpose.

## Modes

### `/update-docs` — incremental, all groups

Cheap and fast. Reads CHANGELOG + git log since last run, figures out what changed, updates only affected docs regardless of which group they belong to.

### `/update-docs <group>` — incremental, scoped

Same as above but only reviews docs from the specified group. Useful when you know you changed something in a specific area and want a quick targeted check.

### `/update-docs full <group>` — deep review

Reads actual source code and compares with every doc in the group. Expensive but thorough — use after major refactors or when a group hasn't been fully reviewed in a while.

A group name is **required** for full mode — there is no "full everything" option. Reading all docs + all their source code in one pass blows up the context. Run groups separately if you need a complete refresh.

Valid groups: `architecture`, `contracts`, `agents`, `ops`, `workflow`.

## State

Last-run metadata lives in `.claude/skills/update-docs/state.json`:

```json
{
  "last_run": "2026-03-10",
  "last_run_mode": "incremental",
  "last_commit": "f3fc97e",
  "groups_full_run": {
    "architecture": "2026-03-08",
    "contracts": "2026-03-10"
  }
}
```

`groups_full_run` tracks when each group last had a full review — useful for deciding which group needs attention next.

If `state.json` doesn't exist — this is the first run. For incremental modes, use the last 2 weeks of git history as baseline. In the summary, note that no group has ever had a full review and suggest starting with the most critical one.

## Doc Groups

Each group bundles logically related docs. The "Code signals" column tells incremental mode which file changes should flag a doc for review.

### `architecture` — System design & topology

| Doc | Code signals |
|-----|-------------|
| `README.md` | new/removed services, major architecture shifts |
| `ARCHITECTURE.md` | `docker-compose.yml`, `services/*/`, queue constants, Caddy config |
| `docs/PIPELINE_V2.md` | consumer flow, queue topology, node transitions |
| `docs/parallel-workers.md` | worker-manager, docker networking, dual-network setup |

### `contracts` — Schemas, terminology & error handling

| Doc | Code signals |
|-----|-------------|
| `docs/CONTRACTS.md` | `shared/contracts/`, `shared/models/`, API router schemas |
| `docs/GLOSSARY.md` | new concepts in CHANGELOG, renamed entities |
| `docs/ERROR_HANDLING.md` | retry logic, error classes, timeout config |

### `agents` — LangGraph nodes & coding agents

| Doc | Code signals |
|-----|-------------|
| `docs/NODES.md` | agent prompts, tools, node definitions, subgraph changes |
| `docs/coding-agents.md` | worker-wrapper, worker-manager, agent config |
| `docs/resource-management.md` | secret handles, worker env, allocation logic |
| `services/langgraph/src/prompts/developer_worker/INSTRUCTIONS.md` | CLI tools, worker-wrapper, task lifecycle |

### `ops` — Deploy, secrets, testing, logging, shared recipes

| Doc | Code signals |
|-----|-------------|
| `docs/DEPLOY.md` | deploy worker, GitHub Actions, Caddy config, infra-service |
| `docs/SECRETS.md` | encryption code, secret-related env vars, GitHub secrets flow |
| `docs/TESTING.md` | Makefile test targets, CI config, pytest setup, new test dirs |
| `docs/LOGGING.md` | `shared/log_config.py`, structlog usage patterns |
| `.claude/skills/shared/pipeline-recipes.md` | queue APIs, worker-manager API, GitHubAppClient, story/task transitions, debug endpoints |

### `workflow` — Dev process & user stories

| Doc | Code signals |
|-----|-------------|
| `docs/DEV_PIPELINE.md` | skills, API routes for tasks/stories, scheduler, consumer changes |

### Not managed by this skill

- `docs/CHANGELOG.md` — updated by `/implement`, not by doc sync
- `docs/backlog.md`, `docs/STATUS.md`, `docs/ROADMAP.md`, `docs/audit.md` — managed by sprint skills
- `docs/sprints/*` — managed by sprint skills (`/plan-phase`, `/implement`, `/close-phase`, `/close-sprint`)
- `docs/e2e_results/*`, `docs/brainstorms/*` — historical records
- `docs/skill-feedback.md` — append-only log
- `.claude/skills/*/SKILL.md` — managed by `/skill-creator`
- `CLAUDE.md`, `AGENTS.md` — meta-config, edited manually

## Protocol

### 0. Load state

Read `state.json`. If missing, tell the user this is the first run and ask which group to start with (or suggest one based on recent CHANGELOG activity).

### 1. Determine scope

**`/update-docs` (incremental, all groups):**

```bash
git log --oneline --since="<last_run>" --name-only
```

Also read `docs/CHANGELOG.md` entries since `last_run` date — the CHANGELOG describes changes in terms of features and components, which is more useful than raw file paths for understanding what docs need updating.

From changed files + CHANGELOG entries, use the "Code signals" column to determine which docs are potentially affected. Only review those docs, regardless of which group they belong to.

If nothing changed — say so and exit.

**`/update-docs <group>` (incremental, scoped):**

Same git log + CHANGELOG analysis, but only consider docs from the specified group. Ignore matches in other groups.

**`/update-docs full <group>` (deep review):**

All docs in the specified group are in scope. Read the actual code areas listed in "Code signals" for each doc — not just what changed recently, but the full current state.

### 2. Review & update each doc

For each doc in scope:

1. **Read the current doc**
2. **Read the relevant code** — use "Code signals" to know where to look. In incremental mode, focus on the specific files that changed. In full mode, do a broader read of the code area.
3. **Compare** — look for: outdated descriptions, missing new features, removed components still mentioned, wrong file paths, stale examples, incorrect command syntax
4. **Update the doc** — edit directly. Keep existing style and structure. Don't rewrite accurate sections.

Guidelines:
- Preserve the author's voice and formatting conventions
- Don't bloat docs with implementation details — keep the same level of abstraction
- If a section is completely wrong, rewrite it; if mostly right, patch it
- When unsure if something changed, check the code rather than guessing
- Update dates/versions where docs show them
- For `ARCHITECTURE.md`: update the ASCII diagram if service topology changed

### 3. Save state

```json
{
  "last_run": "<today>",
  "last_run_mode": "<mode>",
  "last_commit": "<current HEAD sha>",
  "groups_full_run": { "<group>": "<today>", ... }
}
```

For incremental runs, update `last_run` and `last_commit` but don't touch `groups_full_run`.
For full runs, also update the specific group's date in `groups_full_run`.

### 4. Commit

```bash
git add <updated docs> .claude/skills/update-docs/state.json
git commit -m "docs: update living docs (<mode> sync, <N> files)"
```

Do NOT push — doc-only commits stay local to avoid wasting CI minutes.

### 5. Report

```
## Docs Update Summary
- Mode: incremental [<group>] | full <group>
- Docs reviewed: N
- Docs updated: N (list them)
- Docs unchanged: N
- Notable changes: <brief bullets>
- Groups never fully reviewed: <list>
- Stale groups (last full run >2 weeks ago): <list>
```

Always check `groups_full_run` and surface groups that have never been fully reviewed or haven't been reviewed in >2 weeks.

## Tips for Incremental Mode

The CHANGELOG is your best friend — it describes changes in terms of features and components, not just file paths. "Worker-manager mounts workspace by repo_id" tells you to check `docs/parallel-workers.md` and `docs/coding-agents.md` even if those doc files aren't in the git diff.

Quick heuristics:
- `shared/contracts/` changes → check `docs/CONTRACTS.md`
- `docker-compose.yml` changes → check `ARCHITECTURE.md`
- Makefile changes → check `docs/TESTING.md` and `README.md`
- New service directory → check `ARCHITECTURE.md` and `README.md`
- Queue constant changes → check `docs/CONTRACTS.md` and `docs/PIPELINE_V2.md`

## Self-Feedback

If you encountered any of these during the run, add an entry to `docs/skill-feedback.md`:

- A doc should be added to or removed from a group
- The "Code signals" mapping missed something (change X should have flagged doc Y)
- A doc's structure made it hard to update
- A group split doesn't feel right (doc X belongs with group Y instead)

```markdown
## [update-docs] — <today's date>
- **Type**: registry-gap | signal-mismatch | structure | grouping
- **Problem**: <what went wrong>
- **Suggested fix**: <concrete change>
```
