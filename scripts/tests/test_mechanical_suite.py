"""Telegram and model prerequisites are independent for the registered native stand."""

from scripts.stand_run import SUITES, requires_model_sessions
from scripts.stand_telethon_preflight import needs_session


def test_mechanical_suite_requires_telegram_without_model_sessions():
    assert needs_session("mega-noop")
    assert not requires_model_sessions("mega-noop")
    assert SUITES["mega-noop"].target.endswith("::TestMechanicalInstall")


def test_mechanical_budget_covers_native_install_chat_readback_and_cleanup():
    from shared import stand_deadlines as d

    assert SUITES["mega-noop"].timeout_seconds == d.MECHANICAL_SUITE_TIMEOUT_SECONDS
    assert d.MECHANICAL_TEST_BOUNDS.setup_item_seconds == sum(
        seconds for _, seconds in d.MECHANICAL_LIFECYCLE_WAITS
    )
    assert d.MECHANICAL_TEST_BOUNDS.setup_item_seconds + 700 < d.MECHANICAL_SUITE_TIMEOUT_SECONDS
    assert d.MECHANICAL_QA_TIMEOUT > 280 + 30
