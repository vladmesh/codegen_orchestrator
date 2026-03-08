---
name: optimize
description: Process skill feedback entries — auto-fix obvious prompt issues, bring non-obvious ones to user for discussion.
allowed-tools: Bash, Read, Write, Edit, Grep, Glob
argument-hint: "[--dry-run]"
---

# Optimize Skills

Process `docs/skill-feedback.md` and improve skill prompts based on real execution experience.

## Input

- `--dry-run` — show proposed changes without applying them
- No arguments — process all entries

## Protocol

### 1. Load feedback

Read `docs/skill-feedback.md`. Parse all entries below the `<!-- entries below -->` marker.

Each entry has the format:

```markdown
## [skill-name] — YYYY-MM-DD
- **Type**: bug | missing-info | optimization
- **Quote**: exact line or section from the skill that caused the issue
- **Problem**: what went wrong or what was missing
- **Suggested fix**: concrete change to the skill text
```

If there are no entries — STOP: "No feedback to process."

### 2. Categorize

Group entries by skill. For each entry, classify:

**Auto-fixable** (apply without asking):
- Wrong command or path (e.g. skill says `make test-all` but correct is `make test-unit`)
- Missing step that is clearly needed (e.g. "cd to repo before running command")
- Outdated API endpoint or parameter name
- Typos or unclear wording where the fix is obvious

**Needs discussion** (bring to user):
- Structural changes to the skill flow (reordering steps, adding/removing phases)
- Trade-offs where multiple fixes are possible
- Suggestions that might affect other skills
- Changes to the skill's scope or responsibility

### 3. Show plan

Before making any changes, present a summary:

```
## Optimization Plan

### Auto-fix (will apply)
1. [audit] Fix command: `make test-all` → `make test-unit` (line ~42)
2. [implement] Add missing step: check branch before creating new one

### Needs discussion
3. [plan] "Should /plan auto-run /brainstorm if topic is complex?" — affects scope
4. [triage] "Dedup logic misses renamed tasks" — multiple possible fixes
```

Wait for user confirmation before applying auto-fixes. Present discussion items one by one.

### 4. Apply fixes

For each approved auto-fix:
1. Read the skill file
2. Find the **Quote** from the feedback entry
3. Apply the **Suggested fix**
4. Show the diff to the user

### 5. Clean up processed entries

After all entries are processed (applied or discussed), remove them from `docs/skill-feedback.md`.
Keep the file header and `<!-- entries below -->` marker.

### 6. Commit (DO NOT push — doc-only commits stay local to avoid wasting CI minutes)

```bash
git add docs/skill-feedback.md .claude/skills/
git commit -m "optimize: process skill feedback — N fixes applied"
```

### 7. Report

```
## Optimization Report

### Applied
- [skill] change description (entry date)

### Discussed — decided
- [skill] decision made (entry date)

### Discussed — deferred
- [skill] reason for deferral (entry date)

### Discarded
- [skill] reason (entry date)
```
