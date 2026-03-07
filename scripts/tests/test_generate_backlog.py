"""Unit tests for backlog generation script."""

from scripts.generate_backlog import format_backlog


def _wi(tag, title_suffix, status="backlog", priority=0, description=None, plan=None):
    return {
        "id": f"wi-{tag}",
        "project_id": "codegen-orchestrator",
        "type": "feature",
        "title": f"#{tag} {title_suffix}",
        "description": description,
        "plan": plan,
        "status": status,
        "priority": priority,
        "acceptance_criteria": None,
        "current_iteration": 0,
        "max_iterations": 3,
        "created_by": "system",
        "created_at": "2026-03-07T00:00:00Z",
        "updated_at": "2026-03-07T00:00:00Z",
    }


def test_format_backlog_queue_section():
    items = [
        _wi(53, "Compose runner fix", priority=0, description="Fix ports"),
        _wi(52, "Scaffold escaping", priority=1, description="Escape task_description"),
    ]
    result = format_backlog(queue=items, done=[], ideas_text="")
    assert "## Queue" in result
    assert "### #53 Compose runner fix" in result
    assert "### #52 Scaffold escaping" in result
    assert result.index("#53") < result.index("#52")


def test_format_backlog_done_section():
    done = [
        _wi(57, "Implement work item events", status="done"),
        _wi(56, "Next skill via API", status="done"),
    ]
    result = format_backlog(queue=[], done=done, ideas_text="")
    assert "## Done" in result
    assert "#57" in result
    assert "#56" in result


def test_format_backlog_ideas_section():
    ideas = "- Project Name Collision\n- Self-hosted GitLab"
    result = format_backlog(queue=[], done=[], ideas_text=ideas)
    assert "## Ideas" in result
    assert "Project Name Collision" in result


def test_format_backlog_ideas_strips_heading():
    """Ideas file with its own heading should not duplicate ## Ideas."""
    ideas = "# Ideas\n\nSome description.\n\n- Idea one\n- Idea two"
    result = format_backlog(queue=[], done=[], ideas_text=ideas)
    assert result.count("## Ideas") == 1
    assert "# Ideas" not in result.split("## Ideas")[1].split("\n")[0]
    assert "Idea one" in result


def test_format_backlog_empty():
    result = format_backlog(queue=[], done=[], ideas_text="")
    assert "# Backlog" in result
    assert "## Queue" in result
    assert "## Done" in result


def test_format_backlog_priority_labels():
    items = [
        _wi(1, "Critical task", priority=0, description="Urgent"),
        _wi(2, "High task", priority=1, description="Important"),
        _wi(3, "Medium task", priority=2, description="Normal"),
    ]
    result = format_backlog(queue=items, done=[], ideas_text="")
    assert "CRITICAL" in result or "critical" in result.lower()


def test_format_backlog_with_plan_link():
    items = [_wi(58, "Triage via API", plan="## Steps\n1. Do thing")]
    result = format_backlog(queue=items, done=[], ideas_text="")
    assert "Plan" in result
