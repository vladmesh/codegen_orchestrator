---
name: brainstorm
description: Structured thinking session on a topic. Creates docs/brainstorms/<topic>.md with Status tracking and Action Items for later triage.
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
- Check existing brainstorms via API: `curl -sf "http://localhost:8000/api/brainstorms/"`

### 2. Create brainstorm in DB

Resolve project UUID and register the brainstorm via API:
```bash
API="http://localhost:8000"
PROJECT_ID=$(curl -sf "$API/api/projects/" | jq -r '.[0].id')
BS=$(curl -sf -X POST "$API/api/brainstorms/" \
  -H "Content-Type: application/json" \
  -d '{
    "project_id": "'"$PROJECT_ID"'",
    "title": "<Topic>",
    "created_by": "claude"
  }')
BS_ID=$(echo "$BS" | jq -r '.id')
```

### 3. Create brainstorm document

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

### 4. Sync content to DB

After writing the document, update the brainstorm content in the DB:
```bash
CONTENT=$(cat docs/brainstorms/<topic-slug>.md | jq -Rs .)
curl -sf -X PATCH "$API/api/brainstorms/$BS_ID" \
  -H "Content-Type: application/json" \
  -d "{\"content\": $CONTENT}"
```

### 5. Interactive discussion

After writing the initial document, present the key findings and open questions to the user.

If the user wants to continue discussing — update the document with new insights (and sync to DB via PATCH).

When the discussion is complete:
1. Set `Status: done` in the markdown header
2. Mark done in DB: `curl -sf -X POST "$API/api/brainstorms/$BS_ID/done" -H "Content-Type: application/json" -d '{"actor": "claude"}'`

### 6. Commit (DO NOT push — doc-only commits stay local to avoid wasting CI minutes)

```bash
git add docs/brainstorms/<topic-slug>.md
git commit -m "brainstorm: <topic>"
```

### 7. Important

- Brainstorms are for **thinking**, not deciding. The user makes final decisions.
- Always end with concrete Action Items — a brainstorm without action items is wasted work.
- Don't create backlog tasks directly. That's `/triage`'s job. Just write Action Items.
- If the brainstorm reveals the topic is simple enough to just do — say so. Not everything needs a brainstorm.

## Self-Feedback

After completing this skill, if you encountered any of the following — add an entry to `docs/skill-feedback.md`:

- A command or path in this skill was **wrong or outdated**
- A step was **missing context** that you had to figure out yourself
- A step could be **simplified or reordered** for better flow
- The skill **gave ambiguous instructions** that led to a wrong first attempt

Entry format:

```markdown
## [brainstorm] — <today's date>
- **Type**: bug | missing-info | optimization
- **Quote**: "<exact line or section from this skill>"
- **Problem**: <what went wrong or was missing>
- **Suggested fix**: <concrete change to the skill text>
```

Only write feedback that is **specific and actionable**. Skip vague impressions.
