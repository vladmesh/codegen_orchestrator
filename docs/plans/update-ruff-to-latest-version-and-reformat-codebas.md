# Update Ruff to latest version and reformat codebase

> [!WARNING]
> Этот файл автогенерируется командой `make sync`. Не редактируйте вручную — изменения будут перезаписаны.

## Context

Ruff is pinned to 0.8.4 in four places (CI workflow, tooling Dockerfile, pre-commit config, and effectively via the tooling container in Makefile). The uv.lock already resolves to 0.15.2, and local ruff is 0.15.1. Bumping to latest and reformatting is a mechanical task with low risk.

Current state: `ruff check` passes clean on 0.15.x, but `ruff format` would reformat 17 files (style changes between 0.8→0.15). No new lint violations.

Note: CLAUDE.md lists rules `S, PLR, C901` but pyproject.toml only enables `E, F, I, UP, B, C4`. This discrepancy is out of scope for this task.

## Steps

1. [ ] Update version pins to latest ruff
   - **Input**: `.github/workflows/ci.yml`, `tooling/Dockerfile`, `.pre-commit-config.yaml`, `pyproject.toml`
   - **Output**: All four files reference the same new ruff version (use latest stable, currently ~0.15.x). CI uses `ruff==0.15.2`, Dockerfile `ruff==0.15.2`, pre-commit `rev: v0.15.2`, pyproject.toml `ruff>=0.15.0`
   - **Test**: `grep -r "0.8.4" . --include="*.yml" --include="*.yaml" --include="*.toml" --include="Dockerfile"` returns nothing

2. [ ] Run ruff format across codebase
   - **Input**: All Python files
   - **Output**: `ruff format .` applies style changes to ~17 files. Commit separately from version pin changes for clean git blame.
   - **Test**: `ruff format --check .` exits 0

3. [ ] Run ruff check and fix any new violations
   - **Input**: All Python files
   - **Output**: `ruff check .` exits 0. If new violations appear (unlikely — already verified clean), fix them.
   - **Test**: `ruff check .` exits 0

4. [ ] Rebuild tooling image and verify make targets
   - **Input**: `tooling/Dockerfile`, Makefile
   - **Output**: `make lint` and `make format` work with updated tooling image (new ruff version baked in)
   - **Test**: `make lint` exits 0

5. [ ] Run unit tests to confirm no breakage
   - **Input**: All test files
   - **Output**: `make test-unit` passes — formatting changes did not break any tests
   - **Test**: `make test-unit` exits 0

6. [ ] Regenerate uv.lock
   - **Input**: `pyproject.toml`, `uv.lock`
   - **Output**: `make lock-deps` regenerates lock file with consistent ruff version
   - **Test**: `uv.lock` contains the target ruff version

