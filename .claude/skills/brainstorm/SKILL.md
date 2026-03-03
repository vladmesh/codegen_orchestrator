---
name: brainstorm
description: Structured thinking session on a topic. Creates docs/brainstorms/<topic>.md with Status tracking and Action Items for later triage.
disable-model-invocation: true
allowed-tools: Bash, Read, Write, Edit, Grep, Glob
argument-hint: "<topic>"
---

# Brainstorm

Structured thinking session. Explores a topic, considers trade-offs, produces a document with actionable conclusions.

## Input

- `$ARGUMENTS` — topic description (REQUIRED). E.g. "worker container security", "admin UI architecture".

## Protocol

### 1. Research

Before writing:
- Read relevant code, docs, and existing brainstorms
- Check `docs/backlog.md` for related tasks
- Check `docs/brainstorms/` for prior work on the topic

### 2. Create brainstorm document

Write to `docs/brainstorms/<topic-slug>.md`:

```markdown
# Brainstorm: <Topic>

> **Дата**: <today>
> **Контекст**: <one-line context>
> **Status**: draft

---

<Structured analysis. Typical sections:>

## Current State
<What exists today>

## Problem / Opportunity
<What needs to change and why>

## Options
### Option A: ...
- (+) ...
- (-) ...

### Option B: ...
- (+) ...
- (-) ...

## Recommendation
<Which option and why>

## Action Items
- → idea: "<one-liner>" (if not ready for backlog)
- → new task: "<title>" (if ready for backlog, will be processed by /triage)
- → backlog #XX (if task already exists)
```

### 3. Interactive discussion

After writing the initial document, present the key findings and open questions to the user.

If the user wants to continue discussing — update the document with new insights.

When the discussion is complete, set `Status: done` in the header.

### 4. Commit

```bash
git add docs/brainstorms/<topic-slug>.md
git commit -m "brainstorm: <topic>"
```

### 5. Important

- Brainstorms are for **thinking**, not deciding. The user makes final decisions.
- Always end with concrete Action Items — a brainstorm without action items is wasted work.
- Don't create backlog tasks directly. That's `/triage`'s job. Just write Action Items.
- If the brainstorm reveals the topic is simple enough to just do — say so. Not everything needs a brainstorm.
