# Audit Report — todo_api

## Summary

This report documents issues, workarounds, and suggestions encountered while building the `todo_api` project in the scaffolded framework environment.

---

## Critical Issues

### 1. Project Not Scaffolded

**Expected:** The task says "The project was scaffolded with copier from service-template" and references `services/backend/`, `shared/spec/`, `AGENTS.md`, and `Makefile`.

**Actual:** The workspace contained only `CLAUDE.md` and `README.md`. No `services/` directory, no `shared/`, no `AGENTS.md`, no `Makefile`.

**Impact:** Had to build the entire project structure from scratch, inferring conventions from paste-cache error logs and the system-installed `shared` package.

**Suggestion:** Ensure the copier scaffolding step runs successfully before handing the repo to the developer agent. Add a preflight check that verifies expected directories exist.

### 2. `copier` Not Installed

**Expected:** TASK.md references copier scaffolding.

**Actual:** `copier` binary is not available in the worker environment (`command not found`).

**Impact:** Cannot run scaffolding manually even if the template were available.

**Suggestion:** Either pre-install copier in the worker image or run scaffolding before the developer agent starts.

### 3. AGENTS.md Missing

**Expected:** CLAUDE.md says to "Read `AGENTS.md` if present — for framework patterns and conventions."

**Actual:** File does not exist.

**Impact:** No documented conventions available. Had to infer patterns from paste-cache files and the installed `shared` package.

**Suggestion:** Either ensure AGENTS.md is generated during scaffolding or don't reference it in CLAUDE.md when it won't exist.

---

## Environment Issues

### 4. `pip install` Permission Denied (system packages)

**Error:** `ERROR: Could not install packages due to an OSError: [Errno 13] Permission denied: '/usr/local/lib/python3.12/site-packages/uvicorn'`

**Workaround:** Used `pip install --user` to install into `~/.local/`.

**Impact:** Packages installed to `/home/worker/.local/lib/python3.12/site-packages/` instead of the system site-packages. Required `PYTHONPATH` manipulation to make `pytest` discoverable.

**Suggestion:** Either grant write access to site-packages, pre-install common dependencies (fastapi, uvicorn, sqlalchemy, pytest, httpx), or provide a virtualenv.

### 5. `ruff` Not Pre-installed

**Expected:** CLAUDE.md says "Git hooks run native ruff" and references `make lint EXEC_MODE=native`.

**Actual:** `ruff` was not found on PATH. No git hooks were configured.

**Workaround:** Installed ruff via `pip install --user ruff`.

**Suggestion:** Pre-install ruff in the worker image since it's referenced as a core tool.

### 6. `shared` Package Namespace Conflict

**Expected:** The framework pattern from paste-cache shows imports like `from shared.generated.schemas import ...`, suggesting generated schemas live in `shared/shared/generated/` in the workspace, mapping to `shared.generated` import path.

**Actual:** The `shared` package is installed system-wide at `/usr/local/lib/python3.12/site-packages/shared/` (version 0.1.0). It does NOT contain a `generated/` subdirectory. Any local `shared/` directory in the workspace creates an import collision.

**Workaround:** Created schemas as `services/backend/src/schemas.py` with import path `services.backend.src.schemas` to avoid the conflict entirely.

**Suggestion:** Clarify the intended import strategy for generated schemas. Consider using a distinct package name (e.g., `project_shared`) or ensure the system `shared` package has a `generated/` directory that gets populated by `make generate-from-spec`.

### 7. `pytest` Not Directly Runnable via `python -m pytest`

**Detail:** Since pytest was installed with `--user`, `python -m pytest` failed because the system Python doesn't look at user site-packages by default in this environment. Required explicit `PYTHONPATH` including the user site-packages directory.

**Suggestion:** Pre-install test dependencies in the worker image or provide a Makefile that handles PYTHONPATH.

---

## Minor Issues

### 8. `make generate-from-spec` — No Codegen Tool

**Expected:** TASK.md says "Run `make generate-from-spec` after modifying spec files to regenerate code."

**Actual:** No code generation tool is available. There's no scaffolder, no Jinja templates, no codegen script.

**Impact:** The `generate-from-spec` Makefile target is a no-op. All "generated" code was written manually.

**Suggestion:** Either ship the codegen tool in the worker image or document that generated code must be written manually.

### 9. No `.env` or Database Configuration

**Detail:** No `.env` file or database configuration was provided. The app defaults to SQLite (`sqlite+aiosqlite:///./todos.db`) which works for development and testing but isn't production-ready.

**Suggestion:** Include a `.env.example` or document the expected `DATABASE_URL` format in AGENTS.md.

### 10. `orchestrator dev-env` Commands Not Tested

**Detail:** CLAUDE.md documents `orchestrator dev-env start-infra db redis` for infrastructure, but since this is a simple SQLite-based API, these weren't needed. The orchestrator CLI is available but its backend connectivity wasn't verified.

---

## Suggestions for Improvement

1. **Pre-flight validation:** Before handing a repo to the developer, verify that scaffolding completed successfully (check for `services/`, `Makefile`, `AGENTS.md`).

2. **Worker image dependencies:** Pre-install the common stack: `fastapi`, `uvicorn`, `sqlalchemy`, `aiosqlite`, `pytest`, `httpx`, `ruff`, `pytest-asyncio`. These are needed for virtually every backend project.

3. **Consistent import strategy:** Document whether generated schemas should use `shared.generated.schemas` or a service-local path. Resolve the system `shared` package conflict.

4. **Makefile bootstrap:** If the Makefile isn't scaffolded, provide a default one that handles PYTHONPATH, test running, and linting with the correct paths.

5. **Git hooks:** CLAUDE.md references git hooks for ruff, but no hooks were configured in `.git/hooks/`. Either scaffold them or remove the reference.

