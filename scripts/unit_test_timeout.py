"""pytest plugin: a unit test that times out names itself and its asyncio tasks.

`scripts/test-unit-local.sh` loads it (`-p scripts.unit_test_timeout`) next to
`--timeout`/`--timeout-method=thread`. pytest-timeout's thread method dumps every
thread's stack and exits the suite, but it prints neither the node id of the
test that hung nor where its coroutines wait: an event-loop hang shows only the
main thread parked in `select`. This writes both first, then hands over to
pytest-timeout's own dump and exit. Every other method is left to pytest-timeout.
"""

from __future__ import annotations

import asyncio
import io
import threading

import pytest
import pytest_timeout


def _pending_tasks() -> list[asyncio.Task]:
    """Every unfinished task of every loop, a leaked one of a closed loop included."""
    # `asyncio.all_tasks` needs the running loop, and the timer thread has none;
    # this is the set it reads, copied the way it copies it.
    for _ in range(100):
        try:
            return [task for task in list(asyncio.tasks._scheduled_tasks) if not task.done()]
        except RuntimeError:  # the set changed while it was copied
            continue
    return []


def _describe_timeout(item: pytest.Item, settings: pytest_timeout.Settings) -> str:
    out = io.StringIO()
    out.write(f"\nTimeout: {item.nodeid} did not finish within {settings.timeout:g}s\n")
    tasks = _pending_tasks()
    out.write(f"Pending asyncio tasks: {len(tasks)}\n")
    for task in tasks:
        loop = task.get_loop()
        out.write(f"\n--- {task!r} (loop closed: {loop.is_closed()})\n")
        try:
            task.print_stack(file=out)
        except Exception as exc:  # noqa: BLE001 - the thread dump below still runs
            out.write(f"stack unavailable: {exc!r}\n")
    return out.getvalue()


def _expire(item: pytest.Item, settings: pytest_timeout.Settings) -> None:
    terminal = item.config.get_terminal_writer()
    try:
        # Suspended, so this reaches the terminal ahead of the test's own capture.
        capman = item.config.pluginmanager.getplugin("capturemanager")
        if capman:
            capman.suspend_global_capture(item)
        terminal.write(_describe_timeout(item, settings))
        terminal.flush()
    finally:
        pytest_timeout.timeout_timer(item, settings)


@pytest.hookimpl(tryfirst=True)
def pytest_timeout_set_timer(item: pytest.Item, settings: pytest_timeout.Settings) -> bool | None:
    """pytest-timeout's thread timer, with `_expire` in front of its dump."""
    if settings.method != "thread":
        return None
    timer = threading.Timer(settings.timeout, _expire, (item, settings))
    timer.name = f"{__name__} {item.nodeid}"

    def cancel() -> None:
        timer.cancel()
        timer.join()

    item.cancel_timeout = cancel
    timer.start()
    return True
