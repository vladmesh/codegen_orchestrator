"""Unit tests for generate_roadmap.py — format_roadmap function."""

from pathlib import Path
import sys

# Add scripts dir to path so we can import generate_roadmap
sys.path.insert(0, str(Path(__file__).parent.parent))

from generate_roadmap import format_roadmap


def test_format_roadmap_open_milestone_with_items():
    milestones = [
        {
            "id": "ms-1",
            "title": "Phase 1: Foundation",
            "description": "Core pipeline",
            "status": "open",
            "sort_order": 0,
        },
    ]
    tasks_by_milestone = {
        "ms-1": [
            {"title": "#1 Setup CI", "status": "done"},
            {"title": "#2 Deploy pipeline", "status": "backlog"},
        ],
    }
    unsorted_items = []

    result = format_roadmap(milestones, tasks_by_milestone, unsorted_items)

    assert "## Phase 1: Foundation" in result
    assert "Core pipeline" in result
    assert "- [x] #1 Setup CI" in result
    assert "- [ ] #2 Deploy pipeline" in result


def test_format_roadmap_completed_milestone():
    milestones = [
        {
            "id": "ms-1",
            "title": "Phase 1: Done stuff",
            "description": "All done",
            "status": "completed",
            "sort_order": 0,
        },
    ]
    tasks_by_milestone = {"ms-1": []}
    unsorted_items = []

    result = format_roadmap(milestones, tasks_by_milestone, unsorted_items)

    assert "## Phase 1: Done stuff" in result
    assert "COMPLETE" in result


def test_format_roadmap_unsorted_items():
    milestones = []
    tasks_by_milestone = {}
    unsorted_items = [
        {"title": "#99 Orphan task", "status": "backlog"},
    ]

    result = format_roadmap(milestones, tasks_by_milestone, unsorted_items)

    assert "## Backlog" in result
    assert "- [ ] #99 Orphan task" in result


def test_format_roadmap_multiple_milestones_ordered():
    milestones = [
        {
            "id": "ms-1",
            "title": "Phase 1",
            "description": None,
            "status": "completed",
            "sort_order": 0,
        },
        {
            "id": "ms-2",
            "title": "Phase 2",
            "description": "Current work",
            "status": "open",
            "sort_order": 1,
        },
    ]
    tasks_by_milestone = {
        "ms-1": [{"title": "#1 Done task", "status": "done"}],
        "ms-2": [{"title": "#2 Active task", "status": "in_dev"}],
    }
    unsorted_items = []

    result = format_roadmap(milestones, tasks_by_milestone, unsorted_items)

    # Phase 1 should come before Phase 2
    idx1 = result.index("Phase 1")
    idx2 = result.index("Phase 2")
    assert idx1 < idx2


def test_format_roadmap_header():
    result = format_roadmap([], {}, [])
    assert result.startswith("# Roadmap")
    assert "generated" in result
