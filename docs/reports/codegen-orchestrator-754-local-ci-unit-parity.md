# Local unit-runner and CI parity

Date: 2026-07-26

## Finding

Commit `15b8b0c1` passed `make test-unit` on a developer machine because
`scripts/test-unit-local.sh` set `API_BASE_URL=http://localhost:8000`. The developer's Compose
stack had an API listening on that address, so unit tests that read system configuration through
`ConfigStore` consumed real responses. `Fast Checks` in CI runs the same command without an API;
the request fails and the affected router tests surface the unconfigured mock as
`ValidationError: quarantine_reason / Input should be a valid dictionary`.

This is an environment dependency, not a difference in selected tests, import order, or locked
dependencies. The analogous deploy-consumer failure on `5ec29706` was an explicit
`ConfigStoreUnavailableError` after its connection to the absent API was refused.

## Decision

The local runner now sets `API_BASE_URL=http://127.0.0.1:9`, an intentionally unreachable local
endpoint. It keeps the setting required by settings validation while preventing a host Compose API
from making a unit test accidentally pass. No stub is started: it would add a service lifecycle,
configuration surface, and a second approximation of the API to every fast test run.

Tests that need configuration must mock or inject `ConfigStore`. A missed fake now fails locally
with the same unavailable-service behavior as CI. `Fast Checks` remains the authoritative verdict,
and `make test-unit` is its local reproduction command.

`scripts/check-ci-gate.py` locks this rule to the exact unreachable endpoint. Changing the runner
back to a host-service URL makes `make ci-contract` fail.

## Verification

Before the runner change, the new CI-contract assertion failed because the runner used
`http://localhost:8000`. After the change, it passes. In a detached worktree at `15b8b0c1`, changing
only that runner value to `http://127.0.0.1:9` made the API suite fail 18 tests and report the same
`StoryRead.quarantine_reason` `MagicMock` validation error as CI. The full current suite passes with
the new runner setting, so the later test fixes supply the missing values without a live API.
