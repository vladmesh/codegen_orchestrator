"""Unit tests for generate_roadmap.py — format_roadmap function."""

from pathlib import Path
import sys

# Add scripts dir to path so we can import generate_roadmap
sys.path.insert(0, str(Path(__file__).parent.parent))

from generate_roadmap import format_roadmap


def _story(id="s-1", title="Story", description=None, status="active", type="product", tasks=None):
    return {
        "id": id,
        "title": title,
        "description": description,
        "status": status,
        "type": type,
        "tasks": tasks or [],
    }


def test_format_roadmap_product_story_with_tasks():
    stories = [
        _story(
            title="User can deploy projects",
            description="Full deploy pipeline",
            tasks=[
                {"title": "#1 Setup CI", "status": "done"},
                {"title": "#2 Deploy pipeline", "status": "backlog"},
            ],
        ),
    ]

    result = format_roadmap(stories, [])

    assert "## User can deploy projects" in result
    assert "Full deploy pipeline" in result
    assert "- [x] #1 Setup CI" in result
    assert "- [ ] #2 Deploy pipeline" in result


def test_format_roadmap_completed_story():
    stories = [
        _story(title="MVP launch", status="completed"),
    ]

    result = format_roadmap(stories, [])

    assert "## MVP launch" in result
    assert "COMPLETE" in result


def test_format_roadmap_technical_stories_separate_section():
    stories = [
        _story(title="User dashboard", type="product"),
        _story(id="s-2", title="Rust migration", type="technical"),
    ]

    result = format_roadmap(stories, [])

    assert "# Product Roadmap" in result
    assert "## User dashboard" in result
    assert "# Technical Initiatives" in result
    assert "## Rust migration" in result


def test_format_roadmap_unsorted_tasks():
    unsorted = [{"title": "#99 Orphan task", "status": "backlog"}]

    result = format_roadmap([], unsorted)

    assert "## Unlinked Tasks" in result
    assert "- [ ] #99 Orphan task" in result


def test_format_roadmap_ordering_product_before_technical():
    stories = [
        _story(id="s-1", title="Technical thing", type="technical"),
        _story(id="s-2", title="Product thing", type="product"),
    ]

    result = format_roadmap(stories, [])

    idx_product = result.index("Product thing")
    idx_technical = result.index("Technical thing")
    assert idx_product < idx_technical


def test_format_roadmap_header():
    result = format_roadmap([], [])
    assert result.startswith("# Roadmap")
    assert "make sync" in result


def test_format_roadmap_no_tasks_section_when_empty():
    stories = [_story(title="Empty story", description="No tasks yet")]

    result = format_roadmap(stories, [])

    assert "## Empty story" in result
    assert "No tasks yet" in result
    # No checkbox lines
    assert "- [" not in result
