---
description: Scan codebase for dead code, code smells, security issues, test gaps, and directory structure clarity. Accepts optional path argument to scope the audit. Usage — /audit [path-from-root]
---

# Code Audit

Scan the codebase for issues and create actionable tasks.

## Severity Levels

Every finding MUST be tagged with a severity:
- 🔴 **critical** — security risk, data loss potential, broken functionality
- 🟡 **warning** — code smell, maintainability concern, outdated term
- 🔵 **info** — minor style issue, suggestion, nice-to-have

Use severity consistently in all report tables (column `Sev`).

## Arguments
- `path` (optional): relative path from project root to limit audit scope (e.g. `services/langgraph`). Default: full codebase.

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

### 1. Glossary, Architecture & Directory Structure Audit

**Before** scanning code, read `docs/GLOSSARY.md` and `ARCHITECTURE.md` (if exists) to load project terminology and expected structure.

`ARCHITECTURE.md` describes the intended system layout. If the real directory tree diverges from what the architecture doc describes — flag it.

Then dump the directory tree for the audit scope:

```bash
# If path argument given:
find <path> -type f | head -200
# Else full project:
find . -type f -not -path './.git/*' -not -path './node_modules/*' -not -path './.venv/*' -not -path './__pycache__/*' | head -300
```

Check for:

**Naming clarity:**
- Directories/files with vague or ambiguous names (e.g. `utils/`, `helpers/`, `misc/`, `common/`, `stuff/`, `tmp/`, `data/`, `core/`) — especially at top levels. Each directory should clearly communicate what domain concept or responsibility it holds.
- Directory names that don't match any Glossary entity when they logically should (e.g. a directory called `jobs/` when the Glossary uses "Task" and "Run").
- Inconsistent naming conventions (`snake_case` vs `kebab-case` vs `camelCase`) within the same level.

**Glossary alignment:**
- Glossary entities that have **no corresponding directory or file** (missing structure for a known concept).
- Directories that use **outdated terminology** from before renames documented in the Glossary (e.g. `workitems/` instead of `tasks/`).
- Code files or directories whose names **contradict** the Glossary definitions (e.g. a file called `consumer.py` that is actually a service, not a queue consumer).

**Tree readability:**
- Flat directories with 10+ files at the same level without sub-grouping.
- Deep nesting (>4 levels) without clear purpose.
- Mixed concerns: source code, configs, docs, and tests dumped in the same directory.

**Docker-Compose alignment:**
- Parse `docker-compose.yml` for service names.
- Every service in compose should have a corresponding directory in `services/`.
- Every directory in `services/` should have a matching service in compose (flag orphaned dirs).
- If scope is limited to a specific service path, only check that service.

### 2. Code Scan

Check each category (limit to `<path>` if provided):

**Lint:**
- Full scope → `make lint`
- Scoped → `make lint LINT_PATH=<path>` (e.g. `make lint LINT_PATH=services/langgraph`)

**Dead code:**
- Unused imports (caught by lint step above)
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

**Dependency freshness:**
- Find `requirements.txt` / `pyproject.toml` in scope.
- Flag packages without version pins (e.g. bare `requests` instead of `requests>=2.31,<3`).
- If inside a running container for the scoped service, run `pip list --outdated --format=json` and flag packages >2 major versions behind.
- Flag duplicate dependencies across services that have diverging version pins.

### 3. Write report

Write/overwrite `docs/audit.md`:

```markdown
# Code Audit

> **Date**: <today>
> **Scope**: <full | path>

## Summary
- 🔴 Critical: N
- 🟡 Warning: N
- 🔵 Info: N
- **Total**: N issues

| Category | 🔴 | 🟡 | 🔵 |
|----------|----|----|---|
| Directory structure | | | |
| Glossary alignment | | | |
| Docker-Compose alignment | | | |
| Dead code | | | |
| Code smells | | | |
| Security | | | |
| Test gaps | | | |
| Dependency freshness | | | |

## CI Health
...

## Directory Structure & Glossary Alignment
| Sev | Location | Issue | Glossary/Arch term | Action |
|-----|----------|-------|--------------------|--------|
| 🟡 | ... | vague name / outdated term / missing structure | ... | rename / restructure / ignore (reason) |

## Docker-Compose Alignment
| Sev | Service/Dir | Issue | Action |
|-----|-------------|-------|--------|
| 🟡 | ... | orphaned dir / missing service dir | ... |

## Dead Code
| Sev | File | Issue | Action |
|-----|------|-------|--------|
| 🔵 | ... | ... | backlog / fix now / ignore (reason) |

## Code Smells
...

## Security
...

## Test Gaps
...

## Dependency Freshness
| Sev | Service | Package | Issue | Action |
|-----|---------|---------|-------|--------|
| 🟡 | ... | ... | no pin / outdated / diverging versions | ... |
```

### 4. Commit (DO NOT push — doc-only commits stay local to avoid wasting CI minutes)

```bash
git add docs/audit.md
git commit -m "audit: <scope> — <N> issues found"
```

### 5. Report

Print summary to console:
- Total issues found: N
- Broken down by category
- Top 3 most critical findings
