"""The scheduler supervisor is the one publisher of a `retrying` planning record.

Two publishers of the same owed retry — the operator action's immediate publish
and the supervisor — raced each other through Redis in every ordering of guard
and publish tried (card codegen-orchestrator-1387, rounds 2 and 3). The seam was
removed: `retry-planning` only writes the record, and the supervisor's
sequential loop publishes it. These checks keep a second publisher from coming
back unnoticed.
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

#: Every function that publishes an `ArchitectMessage`, and what it publishes for.
#: Only `_publish_due_planning_retry` publishes for a `retrying` planning record;
#: the others start a story that has no planning record to owe yet.
ARCHITECT_PUBLISHERS = {
    # The one publisher of a due `retrying` record.
    ("services/scheduler/src/tasks/supervisor/liveness.py", "_publish_due_planning_retry"),
    # A story stuck in `created` that the architect never picked up.
    ("services/scheduler/src/tasks/supervisor/liveness.py", "supervise_stuck_stories"),
    # The next queued story of a project, once the active one completed.
    ("services/scheduler/src/tasks/story_completion.py", "_trigger_next_story"),
    # The operator's send of a `created` or `reopened` story.
    ("services/api/src/routers/stories.py", "send_to_architect"),
    # The PO starting new work or a reopen.
    ("services/langgraph/src/agents/po/tools_stories.py", "create_story"),
    ("services/langgraph/src/agents/po/tools_stories.py", "reopen_story"),
}


def _source_files() -> list[Path]:
    roots = [ROOT / "shared", *sorted((ROOT / "services").glob("*/src"))]
    return [
        path
        for root in roots
        for path in root.rglob("*.py")
        if "tests" not in path.relative_to(ROOT).parts
    ]


def _publishes_to_architect_queue(call: ast.Call) -> bool:
    func = call.func
    if not (isinstance(func, ast.Attribute) and func.attr == "publish_message"):
        return False
    return (
        bool(call.args)
        and isinstance(call.args[0], ast.Name)
        and (call.args[0].id == "ARCHITECT_QUEUE")
    )


def _architect_publishers() -> set[tuple[str, str]]:
    found: set[tuple[str, str]] = set()
    for path in _source_files():
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            if any(
                isinstance(call, ast.Call) and _publishes_to_architect_queue(call)
                for call in ast.walk(node)
            ):
                found.add((str(path.relative_to(ROOT)), node.name))
    return found


def test_every_architect_publisher_is_declared():
    assert _architect_publishers() == ARCHITECT_PUBLISHERS


def test_the_operator_retry_action_publishes_nothing_and_touches_no_redis():
    path = ROOT / "services/api/src/routers/_story_planning.py"
    tree = ast.parse(path.read_text(), filename=str(path))

    names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    names |= {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
    names |= {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
    }
    modules = {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}

    assert not {"ARCHITECT_QUEUE", "publish_message", "get_redis_client", "redis"} & names
    assert not {module for module in modules if module and "redis" in module}
