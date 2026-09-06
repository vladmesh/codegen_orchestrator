"""Whether this run of the suite has already seen one of its tests fail.

The acceptance artifact retains a failed run's worker bodies, and "failed" is a
statement about the **suite**, not about the pipeline: a combination whose
scaffold, engineering, deploy and QA phases all completed and whose assertion
then failed is a red `stand-e2e` run, and it is exactly the run somebody has to
diagnose after the stand is gone. `run_evidence.classify_outcome` cannot see
that — it reads the control plane's terminal state, which says the pipeline
finished.

So the real signal is recorded here, by the one component that has it: pytest's
own per-test report, fed in from `conftest.pytest_runtest_logreport`. A
module-scoped fixture's finaliser — which is where the evidence collection and
the artifact both happen — runs after the last test of its module, and a test's
`call` report is logged before its teardown, so every assertion of the module
that owns this combination has already been reported by the time the collection
asks.

This is read by exactly one function, `run_evidence.run_failure`, which is where
"did this run succeed" is answered for every reader of that question — the
retention, the artifact's `failure.failed`, its verdict, and through the written
`failed` also the acceptance admission.

Two things this deliberately does not try to be. It is not a *session* verdict:
a later module's failure cannot be known to an earlier module's teardown, and
nothing can make it so. And it is not the only source — a phase that raised
leaves the fixture through its own `finally` during the first test's setup,
before any report exists, and that run is caught by the pipeline's terminal state
instead. The two together are the whole of "this run did not succeed".
"""

from __future__ import annotations

_failed_tests: list[str] = []


def record_test_report(node_id: str, when: str, failed: bool) -> None:
    """Record one pytest phase report. Only a failure is remembered."""
    if not failed:
        return
    entry = f"{node_id}::{when}"
    if entry not in _failed_tests:
        _failed_tests.append(entry)


def suite_failed() -> bool:
    """Whether any test of this run has failed or errored so far."""
    return bool(_failed_tests)


def failed_tests() -> list[str]:
    """The phases that failed, in the order they were reported."""
    return list(_failed_tests)


def reset() -> None:
    """Forget what has been recorded. For tests of this mechanism only."""
    _failed_tests.clear()
