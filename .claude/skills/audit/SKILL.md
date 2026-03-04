---
name: audit
description: Scan codebase for dead code, code smells, security issues, and large files. Creates/updates docs/audit.md and adds actionable items to backlog.
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

### 3. Commit

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
