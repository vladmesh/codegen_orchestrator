---
name: audit
description: Scan codebase for dead code, code smells, security issues, contract violations, and architectural deviations. Creates/updates docs/audit.md and adds actionable items to backlog. Use when user says "audit", "scan", "check code quality", or wants to find hardcoded strings, bypassed abstractions, or drift from shared contracts.
allowed-tools: Bash, Read, Write, Edit, Grep, Glob
argument-hint: "[--scope <path>]"
---

# Code Audit

Scan the codebase for issues and create actionable tasks.

## Input

- `--scope <path>` — limit audit to a specific directory (e.g. `services/langgraph`). Default: full codebase.

## Protocol

### 0. CI Health Check

Before scanning code, check the latest CI run on the default branch:

```bash
gh run list --branch main --limit 1 --json status,conclusion,name,headSha,createdAt,url
```

If the latest run **failed or is not successful**:
- Include a `## CI Health` section at the **top** of the audit report (before Dead Code)
- Show: workflow name, commit SHA, failure date, link to the run
- Fetch failed job logs if needed: `gh run view <run-id> --log-failed | tail -50`
- Categorize: test failure / lint failure / build failure / infra flake
- This counts as an issue in the Summary

If the latest run **passed** — add `## CI Health` with "✅ Last CI run passed (<date>)".

### 1. Scan

Check each category:

**Dead code:**
- Unused imports (run `make lint` — ruff catches these)
- Functions/classes with zero callers (grep for definition, grep for usage)
- Files not imported anywhere
- TODO/FIXME comments that reference completed work

**Code smells:**
- Files > 400 LOC (check with `wc -l`)
- Functions > 50 LOC
- Deeply nested code (> 3 levels)
- Copy-paste patterns (similar code blocks)
- `# noqa` comments that could be fixed instead

**Security:**
- Hardcoded secrets, tokens, passwords (grep for patterns)
- `TODO` comments about encryption/auth
- Exposed ports without auth
- Unsafe `subprocess` calls without input validation

**Contract violations** (see detailed guide in "Contract Violations — How to Scan" below):
- Hardcoded status strings instead of shared enums
- Direct `redis.xadd`/`xread`/`hget`/`hdel` bypassing `RedisStreamClient`
- Hardcoded queue names instead of constants from `shared/queues.py`
- Raw dicts published to streams instead of Pydantic DTOs
- Hardcoded Redis key patterns without centralized constants

**Convention violations:**
- `print()` in service code — must use `structlog` (see CLAUDE.md)
- `os.getenv("VAR", "default")` with fallback values — must fail fast with `RuntimeError` (see CLAUDE.md)
- Cross-service imports (`from services.X import ...`) — services must be isolated, communicate via queues/API only
- `json.dumps(model.model_dump())` instead of `model.model_dump_json()` — redundant serialization

**Test gaps:**
- Source files in `services/*/src/` without corresponding test in `tests/unit/`
- Test files that are skipped (`@pytest.mark.skip`)

### 2. Write report

Write/overwrite `docs/audit.md`:

```markdown
# Code Audit

> **Date**: <today>
> **Scope**: <full | path>

## Summary
- Dead code: N issues
- Code smells: N issues
- Security: N issues
- Contract violations: N issues
- Convention violations: N issues
- Test gaps: N issues

## CI Health
...

## Dead Code
| File | Issue | Action |
|------|-------|--------|
| ... | ... | backlog / fix now / ignore (reason) |

## Code Smells
...

## Security
...

## Contract Violations
| File:Line | Violation | Should be | Severity |
|-----------|-----------|-----------|----------|
| `services/scheduler/src/tasks/task_dispatcher.py:114` | `"todo"` hardcoded | `TaskStatus.TODO.value` | high |
| `services/scheduler/src/tasks/scaffold_trigger.py:77` | direct `redis.xadd()` | `redis_client.publish_message()` | medium |

## Convention Violations
| File:Line | Violation | Rule |
|-----------|-----------|------|
| `services/foo/bar.py:12` | `print("debug")` | Use structlog |
| `services/foo/baz.py:5` | `os.getenv("X", "default")` | No env var defaults |

## Test Gaps
...
```

### 3. Commit (DO NOT push — doc-only commits stay local to avoid wasting CI minutes)

```bash
git add docs/audit.md
git commit -m "audit: <scope> — <N> issues found"
```

### 4. Report

Print summary to console:
- Total issues found: N
- New backlog items created: N
- Already tracked: N
- Ignored (with reasons): N

---

## Contract Violations — How to Scan

This section explains how to efficiently detect contract violations. The key idea: the codebase has well-defined shared abstractions (enums, clients, DTOs, constants), but agents writing code sometimes bypass them with hardcoded literals. These violations compile and work — but they drift from the source of truth and break when enums change.

### Hardcoded status strings

The codebase defines status enums in `shared/contracts/dto/`:
- `TaskStatus`: backlog, todo, in_dev, in_ci, testing, done, blocked, failed, cancelled
- `StoryStatus`: created, in_progress, completed, failed, archived
- `ProjectStatus`: draft, scaffolding, scaffolded, scaffold_failed, developing, testing, deploying, active, maintenance, error, failed, missing, archived
- `RunStatus`: queued, running, completed, failed, cancelled

**How to detect**: Grep for quoted status values in `services/` (exclude tests and migrations — tests may legitimately assert on strings):

```bash
# Status literals that should be enum references
rg -n '"(done|todo|in_dev|in_ci|testing|backlog|blocked|failed|cancelled)"' services/ --type py --glob '!**/tests/**' --glob '!**/migrations/**'
rg -n '"(in_progress|completed|archived|created)"' services/ --type py --glob '!**/tests/**' --glob '!**/migrations/**'
rg -n '"(draft|scaffolding|scaffolded|scaffold_failed|developing|deploying|active|maintenance)"' services/ --type py --glob '!**/tests/**' --glob '!**/migrations/**'
```

**False positives to ignore**: log messages, docstrings, comments, Pydantic `Field(description=...)`. Focus on comparisons (`== "done"`), function arguments (`get_tasks_by_status("todo")`), and dict values.

**Severity**: high — a renamed enum value will silently break all hardcoded comparisons.

### Direct Redis operations bypassing RedisStreamClient

`shared/redis/client.py` provides `RedisStreamClient` with methods like `publish_message(stream, pydantic_model)`, `publish(stream, data)`, `consume()`. Direct access to the underlying `redis` object (e.g., `client.redis.xadd(...)`) bypasses standardized message formatting and logging.

**How to detect**:

```bash
# Direct stream operations (should use RedisStreamClient methods)
rg -n '\.xadd\(|\.xread\(|\.xreadgroup\(' services/ --type py --glob '!**/tests/**'
# Direct hash/key operations on queue-related keys
rg -n '\.hget\(|\.hdel\(|\.hset\(' services/ --type py --glob '!**/tests/**'
```

Then check: is the caller using `redis_client.redis.xadd(...)` (bypass) or `redis_client.publish_message(...)` (correct)?

**Severity**: medium — works but loses message envelope consistency and structured logging.

### Hardcoded queue names

`shared/queues.py` defines all queue names as constants (e.g., `ENGINEERING_QUEUE = "engineering:queue"`). Using string literals instead means a queue rename requires grep-and-replace across the codebase.

**How to detect**:

```bash
rg -n '"(engineering|deploy|scaffold|architect|provisioner):queue"' services/ --type py --glob '!**/tests/**'
rg -n '"worker:commands"' services/ --type py --glob '!**/tests/**'
rg -n '"po:(input|response|proactive)"' services/ --type py --glob '!**/tests/**'
```

**Severity**: medium — works until someone renames a queue constant without updating the literals.

### Raw dicts instead of Pydantic DTOs

Queue messages should use the defined DTOs (`EngineeringMessage`, `ScaffoldMessage`, `DeployMessage`, etc.) from `shared/contracts/queues/`. Publishing raw `{"data": json.dumps(...)}` dicts skips validation and makes the contract implicit.

**How to detect**:

```bash
# Raw dict wrapping pattern
rg -n '\{"data":\s*.*\.model_dump_json\(\)' services/ --type py --glob '!**/tests/**'
rg -n 'xadd.*\{"data":' services/ --type py --glob '!**/tests/**'
```

If a `publish_message()` call exists for that stream's DTO, the raw dict is a violation.

**Severity**: medium — silent schema drift between producer and consumer.

### Hardcoded Redis key patterns

Some services construct Redis keys like `f"story:workers:{story_id}"` or `f"worker:meta:{worker_id}"` inline. These should be centralized constants or helper functions.

**How to detect**:

```bash
rg -n '"story:workers' services/ --type py --glob '!**/tests/**'
rg -n '"worker:(meta|status):' services/ --type py --glob '!**/tests/**'
rg -n 'f"[a-z]+:[a-z]+:\{' services/ --type py --glob '!**/tests/**'
```

**Severity**: low-medium — works but key pattern changes require multi-file grep.

---

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
## [audit] — <today's date>
- **Type**: bug | missing-info | optimization
- **Quote**: "<exact line or section from this skill>"
- **Problem**: <what went wrong or was missing>
- **Suggested fix**: <concrete change to the skill text>
```

Only write feedback that is **specific and actionable**. Skip vague impressions.
