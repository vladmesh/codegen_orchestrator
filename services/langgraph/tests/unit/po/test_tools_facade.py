"""Boundary tests for PO tool composition and domain ownership."""

import ast
from pathlib import Path

_PUBLIC_TOOLS = {
    "get_all_tools",
    "get_budget_balance",
    "notify_user",
    "set_reminder",
    "web_search",
}


def test_callers_import_tools_from_their_owner_modules() -> None:
    root = Path(__file__).resolve().parents[5]
    violations = []
    for directory in (root / "services" / "langgraph", root / "tests"):
        for path in directory.rglob("*.py"):
            if any(part.startswith(".") or part == "__pycache__" for part in path.parts):
                continue
            for node in ast.walk(ast.parse(path.read_text(), filename=str(path))):
                if not isinstance(node, ast.ImportFrom):
                    continue
                if node.module not in {"src.agents.po.tools", "agents.po.tools"}:
                    continue
                for alias in node.names:
                    if alias.name not in _PUBLIC_TOOLS:
                        violations.append(f"{path.relative_to(root)}:{node.lineno}: {alias.name}")
    assert not violations, "Import from the owning PO module:\n" + "\n".join(violations)


def test_tools_facade_does_not_export_domain_or_startup_symbols() -> None:
    from src.agents.po import tools

    retired_names = {
        "_get_api",
        "_get_stream_client",
        "_user_headers",
        "init_po_clients",
        "AVAILABLE_MODULES",
        "HTTP_UNPROCESSABLE",
        "PRODUCT_BRIEF_POINTER_KEY",
        "PO_REMINDERS_KEY",
    }
    retired_names.update(
        tool.name for tool in tools.get_all_tools() if tool.name not in _PUBLIC_TOOLS
    )
    for name in retired_names:
        assert not hasattr(tools, name), name
    assert set(tools.__all__) == _PUBLIC_TOOLS


def test_get_all_tools_preserves_tool_identity_and_order() -> None:
    from src.agents.po import tools, tools_briefs, tools_projects, tools_stories

    assert tools.get_all_tools() == [
        tools_projects.create_project,
        tools_projects.list_projects,
        tools_projects.get_project,
        tools_projects.grant_project_user,
        tools_projects.set_project_secret,
        tools_projects.transfer_project_ownership,
        tools_projects.teardown_project,
        tools_projects.validate_telegram_token,
        tools_briefs.present_product_brief,
        tools_briefs.confirm_product_brief,
        tools_stories.create_story,
        tools_stories.list_stories,
        tools_stories.reopen_story,
        tools_stories.get_story,
        tools_stories.get_run_status,
        tools.get_budget_balance,
        tools.set_reminder,
        tools.notify_user,
        tools.web_search,
    ]
