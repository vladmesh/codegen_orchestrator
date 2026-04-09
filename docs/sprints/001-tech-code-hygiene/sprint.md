# Sprint 001: Tech Code Hygiene

> **Goal**: Clean up code smells, fix suppressions, refactor large modules, harden secret storage
> **Type**: tech
> **Started**: 2026-04-10

## Phase 0: Quick fixes
- Task 1: Rename duckduckgo_search → ddgs → `tasks/phase0-task1-rename-ddgs.md`
- Task 2: Fix noqa suppressions (extract dataclass, lookup table, named constant, specific except) → `tasks/phase0-task2-fix-noqa-suppressions.md`

## Phase 1: Refactoring
- #19 Split github.py client (986 LOC) into submodules by domain (repos, actions, secrets, workflows), facade delegates

## Phase 2: Infra hygiene
- #1046 Allocate ports only for modules that need host exposure (tg_bot doesn't listen → skip)
- #20 API Key & SSH Key Encryption (Fernet via SecretsCipher, TODOs in api_keys.py and servers.py)

## Decisions
_None yet._

## Deferred
_None yet._

## Endgame
- Audit: pending
- E2E: pending
- Fix phase: pending
- Docs: pending
