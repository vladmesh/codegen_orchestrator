# CI Gate per Task vs Single Push at End

**Status**: Open
**Date**: 2026-03-11

## Problem

Two mechanisms do the same thing — verify CI:

1. **Engineering worker CI gate** — after each task, pushes code and waits for `ci.yml` to pass. If it fails, worker fixes and re-pushes. Built into the pipeline.
2. **"Verify CI" task** — architect (or manual) creates a separate task at the end of a story to push and verify CI. Redundant when CI gate is active.

Current state: both run, wasting time and tokens.

## Options

### A) Keep per-task CI gate, remove "verify CI" task
- Push + CI after every task
- Catch errors early, fix while context is fresh
- Slower overall (CI runs N times per story)
- Update architect prompt to never create CI/test verification tasks

### B) Keep single push at end, remove per-task CI gate
- Worker only commits (no push) per task
- Final step or final task does push + CI
- Faster (one CI run per story)
- Harder to fix — context from early tasks may be lost when CI fails at the end

### C) Hybrid — configurable per project
- Default: per-task CI gate (option A)
- Config flag to batch push at end for simple/trusted projects

## Leaning

Option A — per-task CI gate is safer. Fixing a CI failure in context is much easier than debugging after N tasks. Remove the redundant "verify CI" task from architect prompt.

## Action Items

- [ ] Decide on approach
- [ ] Implement chosen approach
- [ ] Update architect prompt if needed
