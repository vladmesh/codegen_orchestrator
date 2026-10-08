"""Exercise the CI injection fixtures without starting their child processes."""

from contextlib import redirect_stdout
from io import StringIO
import sys
import time
from types import ModuleType

import pytest

from shared.telegram_bot_probe import parse_bot_probe_result
from shared.tests.unit import test_telegram_probe_injection as injection


@pytest.mark.parametrize(
    "test_case",
    [
        injection.test_a_hostile_message_and_bot_username_reach_telethon_as_values,
        injection.test_a_hostile_button_press_reaches_telethon_as_values,
        injection.test_hostile_callback_data_stays_a_value_and_presses_nothing,
    ],
    ids=["message", "callback", "unobserved-callback"],
)
@pytest.mark.parametrize("value", list(injection.HOSTILE.values()), ids=list(injection.HOSTILE))
async def test_hostile_probe_values_reach_telethon_under_the_bounded_wait(
    monkeypatch, tmp_path, test_case, value
):
    child = injection.child.__wrapped__(tmp_path)

    def refuse_process(command):
        raise AssertionError(f"an input escaped its literal and attempted a process: {command}")

    monkeypatch.setattr(injection.os, "system", refuse_process)
    # The stub may replace the child's clock when loaded. Keep that change
    # confined to this test when loading it into this process instead.
    monkeypatch.setattr(time, "monotonic", time.monotonic)

    async def run_in_memory(script, env):
        original_clock = time.monotonic
        try:
            for key, env_value in env.items():
                monkeypatch.setenv(key, env_value)
            for path, source in injection.STUB_TELETHON.items():
                name = path.removesuffix(".py").replace("/", ".").removesuffix(".__init__")
                module = ModuleType(name)
                monkeypatch.setitem(sys.modules, name, module)
                exec(source, module.__dict__)  # noqa: S102 - load the existing fake transport
            output = StringIO()
            with redirect_stdout(output):
                exec(script, {})  # noqa: S102 - execute the generated probe against that transport
            return parse_bot_probe_result(output.getvalue())
        finally:
            time.monotonic = original_clock

    monkeypatch.setattr(injection, "_run", run_in_memory)
    await test_case(child, value)
