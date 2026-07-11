from pathlib import Path

from conftest import run_copier
import yaml


def _structure(root: Path) -> list[str]:
    return sorted(
        str(path.relative_to(root)) for path in root.rglob("*") if ".git" not in path.parts
    )


def test_pinned_ref_is_reproducible(tmp_path: Path):
    first = tmp_path / "first"
    second = tmp_path / "second"
    for output in (first, second):
        result = run_copier(
            "gh:vladmesh/service-template",
            output,
            project_name="pinned-template-test",
        )
        assert result.returncode == 0, result.stderr

    first_answers = yaml.safe_load((first / ".copier-answers.yml").read_text())
    second_answers = yaml.safe_load((second / ".copier-answers.yml").read_text())
    assert first_answers["_commit"] == second_answers["_commit"]
    assert _structure(first) == _structure(second)


def test_unknown_ref_fails(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("conftest.TEMPLATE_REF", "does-not-exist-codegen-432")
    result = run_copier(
        "gh:vladmesh/service-template",
        tmp_path / "unknown",
        project_name="unknown-template-ref",
    )
    assert result.returncode != 0
