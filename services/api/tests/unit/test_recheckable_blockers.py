"""Which QA blockers an operator may clear with `recheck-qa`, pinned.

`_RECHECKABLE_BLOCKERS` is the repository's only enumeration of that set, and
its members are the QA failures whose cause is repaired *outside* the code — a
host that came back, an executor that was restarted, a server row that was
corrected. The other exit from `waiting_human_review` is `accept-result`, which
closes the story rather than re-running QA, so a blocker that falls out of this
set silently loses the operator's route back.

That is how a category can regress a recovery path without touching it: giving
an existing failure a better name moves it out of the set unless the set is
updated too. This test is cheap and executable, so the set cannot drift without
somebody saying why.
"""

from __future__ import annotations

from shared.contracts.dto.run_result import QABlockerCategory
from src.routers.stories import _RECHECKABLE_BLOCKERS


def test_an_unreadable_qa_seat_is_repaired_by_an_operator_and_then_rechecked():
    """The stand's own failure for three paid runs, and its documented repair.

    Before it had a name of its own it arrived as `server_unavailable` and was
    recheckable. Its repair — putting the administrative account back on the
    server row — is exactly the outside-the-code kind this set is for.
    """
    assert QABlockerCategory.QA_IDENTITY_UNREADABLE in _RECHECKABLE_BLOCKERS
    assert QABlockerCategory.SERVER_UNAVAILABLE in _RECHECKABLE_BLOCKERS


def test_a_product_verdict_is_never_something_an_operator_rechecks():
    """The set is infrastructure only; nothing here re-runs a failed product check."""
    assert QABlockerCategory.UNKNOWN not in _RECHECKABLE_BLOCKERS
    assert QABlockerCategory.BOT_NOT_LIVE not in _RECHECKABLE_BLOCKERS
