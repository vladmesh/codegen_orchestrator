# CI Pipeline Redesign

## Current Problems

### 1. No gate on PR merge
No branch protection rules — PRs merge without requiring checks to pass.
Tests on `push:main` run **after** code is already in main. Purely informational.

### 2. Images publish without waiting for tests
`build-worker-base` depends only on `detect-changes`, not on `test-unit`.
If tests fail, broken images are already pushed to GHCR with `:latest` tag.

### 3. Redundant work
Tests run on both PR and push-to-main. If PR checks passed, re-running on main is wasted compute.

## Current Dependency Graph

```
                    detect-changes
                    /            \
            test-unit          build-worker-base  ← pushes images, no test gate!
                |
        test-integration
```

## Proposed Design

### PR workflow (gate)
Validates code before merge. Must pass to allow merge.

```
detect-changes → lint
               → test-unit → test-integration
               → build-worker-base (build only, no push — validates Dockerfiles)
```

### Main workflow (publish)
Runs after merge. Only builds and publishes — tests already passed on PR.

```
build-worker-base (push to GHCR)
→ deploy (future)
```

## Implementation Steps

### Step 1: Enable branch protection (GitHub Settings)
- Repository → Settings → Branches → Add rule for `main`
- Enable "Require status checks to pass before merging"
- Select required checks: `Lint (Ruff)`, all `Unit Tests (*)`, `Integration Tests`
- Enable "Require branches to be up to date before merging" (optional, stricter)

### Step 2: Split CI into two workflows

**`.github/workflows/ci.yml`** — runs on `pull_request` only:
```yaml
on:
  pull_request:
    branches: [main, develop]

jobs:
  lint: ...
  test-unit: ...
  test-integration:
    needs: test-unit
    ...
  build-worker-base:
    # Build only, no push — validates Dockerfiles compile
    # Skip claude/factory on PR (need common base in GHCR)
    ...
```

**`.github/workflows/publish.yml`** — runs on `push:main` only:
```yaml
on:
  push:
    branches: [main]

jobs:
  build-worker-base:
    # Build and push all images to GHCR
    ...
  # Future: deploy job
  # deploy:
  #   needs: build-worker-base
  #   ...
```

### Step 3: Fix image push safety
In publish workflow, `build-worker-base` can run unconditionally (no `detect-changes` needed — if it's on main, it should be published). Or keep `detect-changes` to save runner minutes when unrelated code changes.

### Step 4 (future): Add deploy job
```yaml
deploy:
  needs: build-worker-base
  runs-on: ubuntu-latest
  steps:
    - name: Deploy to production
      run: ...
```

## Migration Plan

Can be done incrementally:
1. First: enable branch protection (zero code changes, immediate safety)
2. Then: split workflows when convenient
3. Later: add deploy job

Even just step 1 alone fixes the main problem — broken code can't reach main.
