"""Unit tests for generate_status.py."""

from scripts.generate_status import format_status


def test_format_status_with_in_dev_task():
    data = {
        "stats": {"backlog": 5, "todo": 0, "in_dev": 1, "in_ci": 0, "testing": 0, "done": 10},
        "in_dev": {
            "id": "task-abc",
            "title": "#65 Big feature",
            "status": "in_dev",
            "plan": "some plan",
            "elapsed_minutes": 42.5,
        },
        "events": [
            {
                "created_at": "2026-03-07T10:00:00Z",
                "event_type": "status_change",
                "from_status": "backlog",
                "to_status": "in_dev",
                "details": {},
            },
            {
                "created_at": "2026-03-07T10:05:00Z",
                "event_type": "note",
                "details": {"action": "step_start", "step": 1},
            },
        ],
        "recent_done": [
            {"title": "#64 Implement PR flow", "updated_at": "2026-03-06T12:00:00Z"},
        ],
    }
    result = format_status(data)

    assert "# STATUS" in result
    assert "#65 Big feature" in result
    assert "**Plan**: yes" in result
    assert "42 min" in result
    assert "backlog → in_dev" in result
    assert "step_start" in result
    assert "#64 Implement PR flow" in result
    assert "backlog: 5" in result
    assert "done: 10" in result


def test_format_status_no_task():
    data = {
        "stats": {"backlog": 0, "todo": 0, "in_dev": 0, "done": 0},
        "in_dev": None,
        "events": [],
        "recent_done": [],
    }
    result = format_status(data)

    assert "no task in progress" in result
    assert "_(none)_" in result


def test_format_status_has_quick_links():
    data = {
        "stats": {},
        "in_dev": None,
        "events": [],
        "recent_done": [],
    }
    result = format_status(data)

    assert "[Backlog](backlog.md)" in result
    assert "[Roadmap](ROADMAP.md)" in result
    assert "[Changelog](CHANGELOG.md)" in result
