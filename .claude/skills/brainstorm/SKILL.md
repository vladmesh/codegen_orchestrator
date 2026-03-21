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

**Your role is opponent, not cheerleader.** The user needs someone to stress-test their reasoning, not confirm it.

- **Challenge assumptions.** If the user says "we need X" — ask why. If the reasoning has holes, say so directly.
- **Point out what's wrong** before acknowledging what's right. Lead with problems.
- **Propose counter-arguments** even if you partly agree. Play devil's advocate.
- **Be blunt.** No hedging ("perhaps we could consider..."), no filler ("great point!"). Say "this won't work because..." or "you're overcomplicating this."
- **Flag complexity creep.** If a solution is growing arms and legs — call it out. "This started as X and is now a framework for Y. Do we actually need Y?"
- **Disagree when you disagree.** Blind agreement is worse than useless — it wastes the brainstorm. If you think an idea is bad, say so with reasons.

After writing the initial document, present the key findings **and your honest critique** to the user.

If the user wants to continue discussing — update the document with new insights.

When the discussion is complete:
1. Set `Status: done` in the markdown header

### 4. Commit (DO NOT push — doc-only commits stay local to avoid wasting CI minutes)

```bash
git add docs/brainstorms/<topic-slug>.md
git commit -m "brainstorm: <topic>"
```

### 5. Important

- Brainstorms are for **thinking**, not deciding. The user makes final decisions.
- Always end with concrete Action Items — a brainstorm without action items is wasted work.
- Don't create backlog tasks directly. That's `/triage`'s job. Just write Action Items.
- If the brainstorm reveals the topic is simple enough to just do — say so. Not everything needs a brainstorm.

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
## [brainstorm] — <today's date>
- **Type**: bug | missing-info | optimization
- **Quote**: "<exact line or section from this skill>"
- **Problem**: <what went wrong or was missing>
- **Suggested fix**: <concrete change to the skill text>
```

Only write feedback that is **specific and actionable**. Skip vague impressions.
