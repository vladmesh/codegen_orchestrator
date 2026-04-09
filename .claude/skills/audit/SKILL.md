---
name: audit
description: Scan codebase for dead code, code smells, security issues, contract violations, missing DTOs, and architectural deviations. Creates/updates docs/audit.md and adds actionable items to backlog. Use when user says "audit", "scan", "check code quality", or wants to find hardcoded strings, bypassed abstractions, missing schemas, untyped API boundaries, or drift from shared contracts.
allowed-tools: Bash, Read, Write, Edit, Grep, Glob
argument-hint: "[--scope <path>]"
---

# Code Audit

Scan the codebase for issues and create actionable tasks.

## Key References
- [docs/VISION.md](docs/VISION.md) — architectural invariants (audit MUST check each one)
- [docs/CONTRACTS.md](docs/CONTRACTS.md) — queue registry, shared DTOs (source of truth for contract violations)
- [docs/ERROR_HANDLING.md](docs/ERROR_HANDLING.md) — error categories, retry policies
- [docs/LOGGING.md](docs/LOGGING.md) — structlog patterns to check against

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

### 1. Load VISION.md

Read `docs/VISION.md` fully. It contains two things the audit checks:

**A. Product direction** — the top part describes what the product is, what it should do, and what it explicitly does NOT do ("Что НЕ делаем"). If the sprint introduced code that directly contradicts a non-goal or moves the product in an explicitly rejected direction — flag it in the report under `## Vision Alignment`.

Examples of violations:
- Adding a generic CI/CD feature when VISION says "not a general-purpose CI/CD platform"
- Adding a third programming language when VISION says "Python + Rust only"
- Building a self-hosted installer when VISION says "not self-hosted (пока)"

This is a judgment call, not a grep pattern. Only flag clear contradictions, not edge cases.

**B. Architectural invariants** — the numbered list at the bottom. Each maps to scan categories below. The audit report must explicitly confirm or flag each invariant.

Invariant → Category mapping:
1. No cross-service imports → Convention violations
2. All statuses are enums → Contract violations (hardcoded status strings)
3. Queue messages are Pydantic DTOs → Contract violations (raw dicts)
4. Fail-fast, no fallbacks → Convention violations (.get defaults, or fallback)
5. Worker terminology → Glossary violations
6. Secrets never reach LLM → Security
7. shared/ = contracts only → Convention violations (cross-service imports)
8. structlog only → Convention violations (print())

### 2. Scan

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

**Missing DTOs & schema gaps** (see detailed guide in "Missing DTOs — How to Scan" below):
- API client methods accepting or returning raw `dict` instead of Pydantic models
- Raw dict literals constructed inline for API payloads (project creation, deployments, incidents, etc.)
- API responses consumed as `response.json()["field"]` without model validation
- TypedDicts used as stand-ins where a shared Pydantic DTO would enforce the contract
- Inconsistent typing across services: one client validates through DTOs, another uses raw dicts for the same entity

**Convention violations:**
- `print()` in service code — must use `structlog` (see CLAUDE.md)
- `os.getenv("VAR", "default")` with fallback values — must fail fast with `RuntimeError` (see CLAUDE.md)
- `.get(key, default)` fallbacks that hide missing data — must use direct access or raise
- `or fallback_value` patterns that silently swallow None — must crash if data is missing
- `if status in ("done", "completed", "success")` multi-guess branches — must use single enum value
- Cross-service imports (`from services.X import ...`) — services must be isolated, communicate via queues/API only
- `json.dumps(model.model_dump())` instead of `model.model_dump_json()` — redundant serialization

**Glossary violations** (see [docs/GLOSSARY.md](docs/GLOSSARY.md)):
- Container/variable/log names using "worker" for non-Worker entities (Worker = ephemeral Docker container with CLI agent only)
- Confusing Consumer (queue listener role) with Service (long-lived process) or Worker

**Test gaps:**
- Source files in `services/*/src/` without corresponding test in `tests/unit/`
- Test files that are skipped (`@pytest.mark.skip`)

### 3. Write report

Write/overwrite `docs/audit.md`:

```markdown
# Code Audit

> **Date**: <today>
> **Scope**: <full | path>

## Vision Alignment
- Product direction: OK / CONCERN — <details if concern>
- Non-goals respected: OK / VIOLATION — <details if violation>

## Invariants (from VISION.md)
| # | Invariant | Status | Violations |
|---|-----------|--------|------------|
| 1 | No cross-service imports | OK / VIOLATION | N |
| 2 | Statuses are enums | OK / VIOLATION | N |
| ... | ... | ... | ... |

## Summary
- Dead code: N issues
- Code smells: N issues
- Security: N issues
- Contract violations: N issues
- Missing DTOs & schema gaps: N issues
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

## Missing DTOs & Schema Gaps
| File:Line | Pattern | Suggested DTO | Severity |
|-----------|---------|---------------|----------|
| `services/langgraph/src/clients/api.py:85` | `create_service_deployment(payload: dict)` | `ServiceDeploymentCreate` | high |
| `services/langgraph/src/agents/po/tools.py:117` | inline `{"project_id": ..., "name": ...}` | `RepositoryCreate` | medium |

## Convention Violations
| File:Line | Violation | Rule |
|-----------|-----------|------|
| `services/foo/bar.py:12` | `print("debug")` | Use structlog |
| `services/foo/baz.py:5` | `os.getenv("X", "default")` | No env var defaults |

## Test Gaps
...
```

### 4. Commit (DO NOT push — doc-only commits stay local to avoid wasting CI minutes)

```bash
git add docs/audit.md
git commit -m "audit: <scope> — <N> issues found"
```

### 5. Report

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

## Missing DTOs — How to Scan

This section detects places where code constructs or consumes data as raw dicts, but a Pydantic model should enforce the contract. The difference from "Contract Violations" above: contract violations catch cases where a shared DTO **already exists** but is bypassed. This section catches cases where the DTO **doesn't exist yet** and should be created.

### Untyped API client methods

API clients should accept Pydantic models as input and return validated models as output. Methods with `dict` signatures are schema gaps — the contract lives only in the caller's head.

**How to detect**:

```bash
# Client methods with dict params or return types
rg -n 'def \w+\(.*payload:\s*dict' services/ --type py --glob '!**/tests/**'
rg -n '\) -> dict:|\) -> list\[dict\]:' services/ --type py --glob '!**/tests/**'
```

Then check: does a shared DTO exist for this entity in `shared/contracts/dto/`? If the API has a schema in `services/api/src/schemas/` but there's no matching shared DTO, that's a gap.

**What to report**: the method signature, the entity it operates on, and whether an API schema already exists (makes the fix easier — just promote it to shared).

**Severity**: high for write operations (create/update — invalid data silently accepted), medium for read operations (unvalidated responses).

### Inline dict literals for API payloads

When code builds a `{"project_id": ..., "status": ...}` dict and passes it to an API call, the schema is implicit. If a field is added or renamed in the API, the caller breaks at runtime with no static warning.

**How to detect**:

```bash
# Dict literals passed to HTTP methods
rg -n '\.post\(.*, json=\{' services/ --type py --glob '!**/tests/**'
rg -n '\.put\(.*, json=\{' services/ --type py --glob '!**/tests/**'
rg -n '\.patch\(.*, json=\{' services/ --type py --glob '!**/tests/**'

# Dict literals passed to client methods
rg -n 'await.*\.\w+\(\s*\{' services/ --type py --glob '!**/tests/**' -A2
```

**False positives to ignore**: simple query params like `{"status": enum.value}` used as `params=` (not `json=`). Focus on `json=` payloads with 2+ fields — those are the ones that benefit from a model.

**Severity**: medium-high — works until the API schema changes.

### Unvalidated API responses

When a client does `resp.json()` and indexes into the result (`resp.json()["id"]`), or returns the raw dict to callers, there's no validation that the response matches expectations. Compare with the good pattern: `ProjectDTO.model_validate(resp.json())`.

**How to detect**:

```bash
# Raw json access without model validation
rg -n '\.json\(\)\[' services/ --type py --glob '!**/tests/**'
rg -n 'return.*\.json\(\)$' services/ --type py --glob '!**/tests/**'
```

Then check: is there a DTO for this entity? If yes, the response should be validated through it. If no, that's a gap — both the DTO and the validation are missing.

**Severity**: medium — silent breakage when API response shape changes.

### TypedDicts that should be shared Pydantic DTOs

TypedDicts provide type hints but no runtime validation. If a TypedDict is used at a service boundary (API responses, queue messages), it should be a Pydantic model in `shared/contracts/`.

**How to detect**:

```bash
rg -n 'class \w+\(TypedDict\)' services/ --type py --glob '!**/tests/**'
```

Then check: is this TypedDict used purely internally (LangGraph state — fine), or does it describe data crossing a service boundary (API response shapes — should be a shared DTO)?

**Severity**: low-medium for internal use, high for boundary types.

### Cross-service consistency check

Different services sometimes type the same entity differently. For example, one service might validate `Project` responses through `ProjectDTO`, while another uses raw dicts for the same API endpoint.

**How to detect**: For each entity with a shared DTO, grep for both the DTO usage and raw dict patterns across all services:

```bash
# Example: who uses ProjectDTO vs raw dicts for projects?
rg -n 'ProjectDTO' services/ --type py --glob '!**/tests/**'
rg -n 'projects.*\.json\(\)' services/ --type py --glob '!**/tests/**'
```

If service A validates and service B doesn't for the same entity — flag service B.

**Severity**: medium — inconsistency means one service is protected and another isn't.

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
