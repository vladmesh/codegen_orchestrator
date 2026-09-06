"""Whether this run of the suite has already seen one of its tests fail.

The acceptance artifact classifies its combination, and "failed" is a statement
about the **suite**, not only about the pipeline: one whose scaffold,
engineering, deploy and QA phases all completed and whose assertion then failed
is red, and `run_evidence.classify_outcome` cannot see that — it reads the
control plane's terminal state, which says the pipeline finished. What the
artifact *retains* no longer depends on any of this: a paid run carries its three
worker bodies whatever the outcome.

So the real signal is recorded here, by the one component that has it: pytest's
own per-test report, fed in from `conftest.pytest_runtest_logreport`. A
module-scoped fixture's finaliser — which is where the evidence collection and
the artifact both happen — runs after the last test of its module, and a test's
`call` report is logged before its teardown, so every assertion of the module
that owns this combination has already been reported by the time the collection
asks.

There is a second signal here, and it is the last one that exists: the session's
own exit status, handed in by `conftest.pytest_sessionfinish`. Some ways a suite
fails are not any test's `call` report — a fixture finaliser that raised after
the module's own evidence ran, `cleanup_guard` re-raising a `CleanupError`, a
collection error in another module. Every one of them has been accounted for by
the time pytest computes that status, which is why the artifact is *finalised*
from a session-end hook rather than from the fixture that wrote it: the question
is asked where the answer already exists.

Both are read by exactly one function, `run_evidence.run_failure`, which is where
"did this run succeed" is answered for every reader of that question — the
retention, the artifact's `failure.failed`, its verdict, and through the written
`failed` also the acceptance admission.

What this is not: a *pre-final* verdict anybody may act on. A reader that asks
before the session ends gets what is known so far, which is why the early
artifact write is a crash-safety copy and never the last word.
"""

from __future__ import annotations

_failed_tests: list[str] = []
# None until `pytest_sessionfinish` hands it over: "not asked yet" and "the
# session ended cleanly" are different facts, and only one of them means the run
# succeeded.
_session_exit_status: int | None = None


def record_test_report(node_id: str, when: str, failed: bool) -> None:
    """Record one pytest phase report. Only a failure is remembered."""
    if not failed:
        return
    entry = f"{node_id}::{when}"
    if entry not in _failed_tests:
        _failed_tests.append(entry)


def record_session_exit(status: object) -> None:
    """Record pytest's own exit status. The last and most complete signal."""
    global _session_exit_status
    _session_exit_status = int(status)


def session_exit_status() -> int | None:
    """Pytest's exit status, or None while the session is still running."""
    return _session_exit_status


def suite_failed() -> bool:
    """Whether this run of the suite has failed, as far as is known right now.

    A test that failed, or a session that ended with a non-zero status — which
    covers every ending no test report names: a fixture finaliser that raised
    after its module's evidence ran, `cleanup_guard` re-raising a `CleanupError`,
    a collection error, an internal error.
    """
    return bool(_failed_tests) or bool(_session_exit_status)


def failed_tests() -> list[str]:
    """The phases that failed, in the order they were reported."""
    return list(_failed_tests)


def reset() -> None:
    """Forget what has been recorded. For tests of this mechanism only."""
    global _session_exit_status
    _failed_tests.clear()
    _session_exit_status = None
