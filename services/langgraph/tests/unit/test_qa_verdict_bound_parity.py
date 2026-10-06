"""The stand ledger restates the QA consumer's verdict bound; the two must agree.

`shared.stand_deadlines` cannot import a service module, so it carries the number
itself. This suite can import both.
"""

from shared import stand_deadlines
from src.consumers._qa_runner import QA_TIMEOUT


def test_the_live_qa_wait_is_built_on_the_executor_s_own_verdict_bound() -> None:
    assert stand_deadlines.QA_EXECUTOR_VERDICT_TIMEOUT == QA_TIMEOUT
    assert stand_deadlines.LIVE_QA_RUN_TIMEOUT == (
        stand_deadlines.QA_RUN_TIMEOUT + stand_deadlines.QA_EXECUTOR_VERDICT_TIMEOUT
    )
