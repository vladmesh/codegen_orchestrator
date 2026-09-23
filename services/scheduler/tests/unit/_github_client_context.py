"""Async-context-manager support for GitHub App client mocks.

Not a test module (no `test_` prefix). The scheduler enters `GitHubAppClient()`
with `async with` once per operation, so a mock standing in for the constructed
client has to yield itself when entered for the calls configured on it to be the
ones the code under test makes.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock


def self_entering(client: AsyncMock) -> AsyncMock:
    """Make `async with client` yield `client` and never swallow an exception."""
    client.__aenter__.return_value = client
    client.__aexit__.return_value = False
    return client


def entered_client(github: AsyncMock, recorder: MagicMock, name: str) -> AsyncMock:
    """A constructed client whose context yields `github`, both recorded under `name`.

    The recorder orders the context's enter/exit against every call on `github`,
    so a test can prove each GitHub call ran on the entered client while its pool
    was open.
    """
    context = AsyncMock()
    context.__aenter__.return_value = github
    context.__aexit__.return_value = False
    recorder.attach_mock(context, f"{name}_context")
    recorder.attach_mock(github, name)
    return context


def assert_one_operation_scope(
    recorder: MagicMock, name: str, exc_type: type[BaseException] | None = None
) -> list[str]:
    """The client `name` is entered once and exited once, with every call of it inside.

    Returns the GitHub calls made on the entered client, in order.
    """
    # Uses of a call's return value (e.g. truth-testing a response) are not GitHub calls.
    calls = [c for c in recorder.mock_calls if "()" not in c[0]]
    names = [c[0] for c in calls]
    enter, exit_ = f"{name}_context.__aenter__", f"{name}_context.__aexit__"
    assert names.count(enter) == 1
    assert names.count(exit_) == 1
    # The constructed object is only entered and exited; GitHub calls go to what it yields.
    assert [n for n in names if n.startswith(f"{name}_context.")] == [enter, exit_]
    start, end = names.index(enter), names.index(exit_)
    exit_args = calls[end].args
    assert (exit_args[0] if exit_args else None) is exc_type
    inside, outside = names[start + 1 : end], names[:start] + names[end + 1 :]
    assert all(n.startswith(f"{name}.") for n in inside)
    assert not any(n.startswith(f"{name}.") for n in outside)
    return [n.removeprefix(f"{name}.") for n in inside]
