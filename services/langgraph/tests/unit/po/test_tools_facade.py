"""PO tool composition: the PO graph gets every owner module's tools, in order.

That callers import a tool from its owner module is a lint rule (ruff TID251, ruff.toml).
"""


def test_get_all_tools_preserves_tool_identity_and_order() -> None:
    from src.agents.po import (
        tools,
        tools_briefs,
        tools_capabilities,
        tools_notices,
        tools_projects,
        tools_stories,
    )

    assert tools.get_all_tools() == [
        tools_projects.create_project,
        tools_projects.list_projects,
        tools_projects.get_project,
        tools_projects.grant_project_user,
        tools_projects.get_initial_owner_deployment,
        tools_projects.retry_initial_owner_deployment,
        tools_projects.set_project_secret,
        tools_projects.transfer_project_ownership,
        tools_projects.teardown_project,
        tools_projects.validate_telegram_token,
        tools_capabilities.preview_capabilities,
        tools_briefs.present_product_brief,
        tools_briefs.confirm_product_brief,
        tools_briefs.show_full_brief,
        tools_stories.create_story,
        tools_stories.list_stories,
        tools_stories.reopen_story,
        tools_stories.get_story,
        tools_stories.get_product_situation,
        tools_notices.suppress_owner_notice,
        tools_notices.resolve_deferred_notice,
        tools_stories.record_unverified_decision,
        tools_stories.get_story_diagnostics,
        tools_stories.get_run_status,
        tools.get_budget_balance,
        tools.set_reminder,
        tools.notify_user,
        tools.note_to_admins,
        tools.pass_capability_request,
        tools.web_search,
    ]
