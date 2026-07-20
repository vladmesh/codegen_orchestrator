---
name: architect
description: Decompose a Story into Tasks. Reads story from API, analyzes scope, creates concrete tasks with descriptions and acceptance criteria.
allowed-tools: Bash, Read, Write, Edit, Grep, Glob
argument-hint: "[story-ID]"
---

# Architect — Story Decomposition into Tasks

> **⚠️ Requires running orchestrator.** This skill reads stories and creates tasks via the API.
> Before starting, verify: `curl -sf http://localhost:8000/api/projects/ > /dev/null && echo "API OK" || echo "API NOT RUNNING — run 'make up' first"`

Takes a Story and decomposes it into actionable Tasks via the API.

## Key References
- [docs/DEV_PIPELINE.md](docs/DEV_PIPELINE.md) — task lifecycle, status transitions, decomposition patterns

## Input

- `story-ID` — story ID to decompose (e.g. `story-abc123`). If omitted, picks the next `created` story by priority.

## Protocol

### 1. Load story

**If story ID given:**
```bash
API="http://localhost:8000"
STORY=$(curl -sf "$API/api/stories/$STORY_ID")
```

**If no argument — auto-pick next created story:**
```bash
API="http://localhost:8000"
STORY=$(curl -sf "$API/api/stories/?status=created" \
  | jq 'sort_by(.priority) | .[0]')
STORY_ID=$(echo "$STORY" | jq -r '.id')
```

If no `created` stories exist — STOP: "No stories to decompose. Create one via API."

Print: "Decomposing: **$(echo "$STORY" | jq -r '.title')** (priority $(echo "$STORY" | jq -r '.priority'))"

### 2. Load context

Gather information needed to decompose the story:

**Story details**: read `title`, `description`, `acceptance_criteria`, `type` (product/technical), `blocked_by_story_id`, `parent_story_id` from the response.

**Existing tasks for this story** (avoid duplicates):
```bash
EXISTING=$(curl -sf "$API/api/tasks/?story_id=$STORY_ID")
echo "$EXISTING" | jq -r '.[] | "#\(.title) [\(.status)]"'
```

If tasks already exist, print them and ask: "This story already has N tasks. Create additional tasks, or skip?"

**Child stories** (if this is a parent story):
```bash
CHILDREN=$(curl -sf "$API/api/stories/" | jq '[.[] | select(.parent_story_id == "'"$STORY_ID"'")]')
```

**All stories** (for cross-story dependency awareness):
```bash
ALL_STORIES=$(curl -sf "$API/api/stories/" | jq -r '.[] | select(.status != "archived") | "\(.id) | \(.title) [\(.status)]"')
```

**Project and repo context**:
```bash
PROJECT_ID=$(curl -sf "$API/api/projects/" | jq -r '.[0].id')
REPOS=$(curl -sf "$API/api/repositories/?project_id=$PROJECT_ID")
```

**Codebase exploration**: read relevant files based on the story description — architecture docs, existing implementations, related services. Use Grep/Glob to find relevant code.

### 3. Decompose

Analyze the story and break it into concrete tasks. For each task, determine:

- **Title**: `#<TAG> <descriptive title>` (get tag from `GET /api/tasks/next-tag`)
- **Type**: `feature`, `bug`, `chore`, or `refactor`
- **Description**: what needs to be done, specific enough for `/plan` to create steps
- **Acceptance criteria**: optional, for complex tasks
- **Priority**: inherit from story, adjust per task (0=critical, 1=high, 2=medium, 3=low)
- **Dependencies**: if task B depends on task A, set `blocked_by_task_id` on B after A is created
- **need_e2e**: true if the task touches core pipeline or integration points

Guidelines:
- Each task should be completable in 1-3 focused sessions
- Tasks should have clear boundaries — no overlapping scope
- Order tasks by dependency (foundational work first)
- Include both implementation and testing tasks where needed
- Match the story's `acceptance_criteria` — every criterion should be covered by at least one task

### 4. Create tasks via API

For each task, get the next tag and create:

```bash
NEXT_TAG=$(curl -sf "$API/api/tasks/next-tag" | jq -r '.next_tag')

curl -sf -X POST "$API/api/tasks/" \
  -H "Content-Type: application/json" \
  -d '{
    "project_id": "'"$PROJECT_ID"'",
    "title": "#'"$NEXT_TAG"' <Task title>",
    "type": "<type>",
    "description": "<description>",
    "acceptance_criteria": "<criteria or null>",
    "priority": <N>,
    "need_e2e": <true|false>,
    "story_id": "'"$STORY_ID"'",
    "created_by": "architect"
  }'
```

Save the created task ID. If another task depends on this one, use its ID for `blocked_by_task_id`:

```bash
# For dependent tasks:
curl -sf -X POST "$API/api/tasks/" \
  -H "Content-Type: application/json" \
  -d '{
    "project_id": "'"$PROJECT_ID"'",
    "title": "#'"$NEXT_TAG"' <Dependent task>",
    "type": "<type>",
    "description": "<description>",
    "priority": <N>,
    "story_id": "'"$STORY_ID"'",
    "blocked_by_task_id": "<blocker_task_id>",
    "created_by": "architect"
  }'
```

### 5. Transition story to in_progress

After creating all tasks:
```bash
curl -sf -X POST "$API/api/stories/$STORY_ID/start" \
  -H "Content-Type: application/json" \
  -d '{"actor": "architect"}'
```

### 6. Commit (DO NOT push — doc-only commits stay local to avoid wasting CI minutes)

Tasks live in the API; commit any docs you touched by hand.

```bash
git add docs/backlog.md docs/ROADMAP.md docs/STATUS.md
git commit -m "architect: decompose story — $(echo "$STORY" | jq -r '.title')"
```

### 8. Report

Print a summary:

```
## Architect Report

**Story**: <title> (priority <N>)

### Tasks Created
| # | Title | Type | Priority | Blocked By |
|---|-------|------|----------|------------|
| <tag> | <title> | <type> | <priority> | — |
| <tag> | <title> | <type> | <priority> | #<blocker_tag> |

### Dependency Graph
<visual representation if there are dependencies>

### Next Steps
- Run `/plan #<first_task_tag>` to plan the first unblocked task
- Or run `/implement` to auto-pick and start working
```

## Important

- **Every task MUST have `story_id`** — this links it back to the story being decomposed.
- **Dedup before creating** — check existing tasks for the story. Don't create duplicates.
- **Don't over-decompose** — 3-8 tasks per story is typical. If you need more, consider whether the story should be split into child stories instead.
- **Ask for confirmation** before creating tasks if the decomposition is ambiguous or the story description is vague.

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
## [architect] — <today's date>
- **Type**: bug | missing-info | optimization
- **Quote**: "<exact line or section from this skill>"
- **Problem**: <what went wrong or was missing>
- **Suggested fix**: <concrete change to the skill text>
```

Only write feedback that is **specific and actionable**. Skip vague impressions.
