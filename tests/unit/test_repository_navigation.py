"""Repository navigation stays discoverable without duplicated agent guidance."""

from pathlib import Path
import subprocess

ROOT = Path(__file__).parents[2]


def test_readme_maps_the_top_level_areas_people_need_to_navigate():
    readme = (ROOT / "README.md").read_text()

    assert "## Repository map" in readme
    for path in (
        "services/",
        "shared/",
        "tests/",
        "tests/compose/",
        "infra/",
        "scripts/",
        "docs/",
    ):
        assert path in readme


def test_repository_has_no_legacy_claude_skill_tree():
    """Nothing under `.claude` is tracked.

    The check is on the index, not the working tree: an agent running locally may
    leave untracked scratch state there without that being a repository fact.
    """
    tracked = subprocess.run(
        ["git", "ls-files", ".claude"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout

    assert tracked.strip() == ""


def test_claude_entrypoint_delegates_to_the_canonical_agent_playbook():
    agents = (ROOT / "AGENTS.md").read_text()
    claude = (ROOT / "CLAUDE.md").read_text()

    assert "# Agents Playbook" in agents
    assert "[AGENTS.md](AGENTS.md)" in claude
    assert "canonical" in claude.lower()
    assert "Do not add project rules here" in claude
    assert "## " not in claude
