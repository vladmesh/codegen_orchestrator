# Local unit-runner and CI parity

Date: 2026-07-26

## Findings

There are two independent failures; the initial report incorrectly combined them.

### `15b8b0c1`: API schema fixture defect

`services/api` and `test_stories_router.py` do not import `ConfigStore`. The CI failure on
`15b8b0c1` comes from the new `StoryRead.quarantine_reason: dict | None` field and fixtures that
create an unrestricted `MagicMock` without setting that attribute. Attribute lookup therefore
returns another `MagicMock`, which Pydantic rejects as a dictionary. Commit
`c7d5e6dcaed2aa8e09f038a14b28dff49ecf5a10` fixes that exact defect by setting
`quarantine_reason = None` in the router and schema fixtures.

This failure is independent of `API_BASE_URL`: on the detached `15b8b0c1` source,
`test_story_schemas.py::TestStoryRead::test_from_attributes` failed with the same validation error
both at `http://localhost:8000` and at `http://127.0.0.1:9` using Python 3.12.3 and Pydantic
2.13.4. Replacing the runner URL can expose the failure in a clean checkout, but it does not cause
it.

The reported green developer run on that SHA cannot be reproduced or attributed more precisely.
The commit has no tracked `uv.lock` because the root `.gitignore` excludes it, and the original
run did not record its virtualenv or resolved dependency versions. CI runs a fresh `uv sync`; the
remaining material difference is therefore the unrecorded dependency environment. This task does
not claim that a live API made the API-router tests green. For this historical SHA, `Fast Checks`
is authoritative; a clean checkout followed by `uv sync` and `make test-unit` is the closest local
reproduction.

### ConfigStore consumers: host API leakage

The runner formerly set `API_BASE_URL=http://localhost:8000`. A developer's Compose API can answer
requests from tests that read system configuration through `ConfigStore`, while CI has no API.
This is the proven mechanism behind the deploy-consumer class seen on `5ec29706`, where CI raised
`ConfigStoreUnavailableError` after connection refusal. It is separate from the API schema defect
above.

## Decision

The local runner now sets `API_BASE_URL=http://127.0.0.1:9`, an intentionally unreachable local
endpoint. It keeps the setting required by settings validation while preventing a host Compose API
from making a unit test accidentally pass. No stub is started: it would add a service lifecycle,
configuration surface, and a second approximation of the API to every fast test run.

Tests that need configuration must mock or inject `ConfigStore`. A missed fake now fails locally
with the same unavailable-service behavior as CI. `Fast Checks` remains the authoritative verdict,
and `make test-unit` is its local reproduction command for this ConfigStore-dependent class.

`scripts/check-ci-gate.py` locks this rule to the exact unreachable endpoint. Changing the runner
back to a host-service URL makes `make ci-contract` fail.

## Verification

Before the runner change, the new CI-contract assertion failed because the runner used
`http://localhost:8000`; it passes after the change. The detached-commit targeted reproduction
above failed with the same `StoryRead.quarantine_reason` error under both URLs, proving the two
cases are distinct. The current full suite passes with the unreachable endpoint because the later
fixture fix supplies `quarantine_reason = None`.
