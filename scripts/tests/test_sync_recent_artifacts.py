"""Unit tests for sync_recent_artifacts.py."""

from scripts.sync_recent_artifacts import _slugify, sync_artifacts


def test_slugify_with_tag():
    assert _slugify("#65 Big Feature Name") == "65-big-feature-name"


def test_slugify_without_tag():
    assert _slugify("make sync — docs generation") == "make-sync-docs-generation"


def test_sync_writes_plans(tmp_path):
    plans_dir = tmp_path / "plans"
    bs_dir = tmp_path / "brainstorms"

    tasks = [
        {"title": "#65 Big feature", "plan": "## Steps\n1. Do stuff", "source_brainstorm_id": None},
    ]
    result = sync_artifacts(tasks, {}, plans_dir, bs_dir)

    assert len(result["written"]) == 1
    plan_file = plans_dir / "65-big-feature.md"
    assert plan_file.exists()
    assert "## Steps" in plan_file.read_text()


def test_sync_writes_brainstorm(tmp_path):
    plans_dir = tmp_path / "plans"
    bs_dir = tmp_path / "brainstorms"

    tasks = [
        {
            "title": "#66 Some task",
            "plan": "a plan",
            "source_brainstorm_id": "bs-abc",
        },
    ]
    brainstorms = {
        "bs-abc": {"title": "Brainstorm about X", "content": "We discussed Y."},
    }
    result = sync_artifacts(tasks, brainstorms, plans_dir, bs_dir)

    assert len(result["written"]) == 2  # noqa: PLR2004
    bs_file = bs_dir / "66-some-task.md"
    assert bs_file.exists()
    assert "We discussed Y." in bs_file.read_text()


def test_sync_deletes_old_files(tmp_path):
    plans_dir = tmp_path / "plans"
    bs_dir = tmp_path / "brainstorms"
    plans_dir.mkdir()
    bs_dir.mkdir()

    # Pre-existing old files
    (plans_dir / "old-plan.md").write_text("old")
    (bs_dir / "old-brainstorm.md").write_text("old")

    tasks = [
        {"title": "#65 New task", "plan": "new plan", "source_brainstorm_id": None},
    ]
    result = sync_artifacts(tasks, {}, plans_dir, bs_dir)

    assert len(result["deleted"]) == 2  # noqa: PLR2004
    assert not (plans_dir / "old-plan.md").exists()
    assert not (bs_dir / "old-brainstorm.md").exists()
    assert (plans_dir / "65-new-task.md").exists()


def test_sync_no_plan_no_file(tmp_path):
    plans_dir = tmp_path / "plans"
    bs_dir = tmp_path / "brainstorms"

    tasks = [
        {"title": "#65 Task without plan", "plan": None, "source_brainstorm_id": None},
    ]
    result = sync_artifacts(tasks, {}, plans_dir, bs_dir)

    assert len(result["written"]) == 0
    assert len(list(plans_dir.glob("*.md"))) == 0


def test_sync_empty_tasks_cleans_all(tmp_path):
    plans_dir = tmp_path / "plans"
    bs_dir = tmp_path / "brainstorms"
    plans_dir.mkdir()
    bs_dir.mkdir()

    (plans_dir / "stale.md").write_text("stale")

    result = sync_artifacts([], {}, plans_dir, bs_dir)

    assert len(result["deleted"]) == 1
    assert not (plans_dir / "stale.md").exists()
