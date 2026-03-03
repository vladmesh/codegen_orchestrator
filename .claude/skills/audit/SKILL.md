---
name: audit
description: Scan codebase for dead code, code smells, security issues, and large files. Creates/updates docs/audit.md and adds actionable items to backlog.
disable-model-invocation: true
allowed-tools: Bash, Read, Write, Edit, Grep, Glob
argument-hint: "[--scope <path>]"
---

# Code Audit

Scan the codebase for issues and create actionable tasks.

## Input

- `--scope <path>` — limit audit to a specific directory (e.g. `services/langgraph`). Default: full codebase.

## Protocol

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
- Test gaps: N issues

## Dead Code
| File | Issue | Action |
|------|-------|--------|
| ... | ... | backlog / fix now / ignore (reason) |

## Code Smells
...

## Security
...

## Test Gaps
...
```

### 3. Report

Print summary to console:
- Total issues found: N
- New backlog items created: N
- Already tracked: N
- Ignored (with reasons): N
