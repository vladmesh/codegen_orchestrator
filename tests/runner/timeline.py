"""The runner proof's wall-clock timeline: every stage, wait and executor command, start to end.

Entries are appended when they start, so a failed or unfinished proof keeps the span it was in.
Each carries its kind, name, optional detail, thread, UTC start and end, seconds and outcome.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
import contextlib
from datetime import UTC, datetime
import functools
import inspect
import threading
import time
from types import ModuleType
from typing import Any


def now() -> str:
    return datetime.now(UTC).isoformat()


class Timeline:
    def __init__(self, entries: list[dict]) -> None:
        self.entries = entries

    @contextlib.contextmanager
    def span(self, kind: str, name: str, detail: str | None = None) -> Iterator[dict]:
        entry: dict[str, Any] = {"kind": kind, "name": name}
        if detail is not None:
            entry["detail"] = detail
        entry |= {"thread": threading.current_thread().name, "started_at": now()}
        self.entries.append(entry)
        started = time.monotonic()
        try:
            yield entry
        except BaseException:
            entry["outcome"] = "failed"
            raise
        else:
            entry["outcome"] = "passed"
        finally:
            entry["finished_at"] = now()
            entry["seconds"] = round(time.monotonic() - started, 3)


def stage(name: str, method: Callable, detail: int | None = None) -> Callable:
    """`method` as one `stage` span of `self.timeline`, sync or async.

    `detail` is the index of the positional argument that names which call this is.
    """

    def described(args: tuple) -> str | None:
        return None if detail is None else str(args[detail])

    if inspect.iscoroutinefunction(method):

        @functools.wraps(method)
        async def timed_async(self, *args: Any, **kwargs: Any) -> Any:
            with self.timeline.span("stage", name, described(args)):
                return await method(self, *args, **kwargs)

        return timed_async

    @functools.wraps(method)
    def timed(self, *args: Any, **kwargs: Any) -> Any:
        with self.timeline.span("stage", name, described(args)):
            return method(self, *args, **kwargs)

    return timed


@contextlib.contextmanager
def commands(
    timeline: Timeline, module: ModuleType, describe: Callable[[list[str]], str]
) -> Iterator[None]:
    """Every `module._run_cmd(argv, ...)` call is an `executor` span while this is open."""
    run_cmd = module._run_cmd

    async def timed(argv: list[str], *args: Any, **kwargs: Any) -> Any:
        with timeline.span("executor", describe(argv)):
            return await run_cmd(argv, *args, **kwargs)

    module._run_cmd = timed
    try:
        yield
    finally:
        module._run_cmd = run_cmd
