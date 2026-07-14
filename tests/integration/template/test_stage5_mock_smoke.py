"""Stage 5 deterministic smoke for the pinned service-template."""

from pathlib import Path

from stage5_mock_smoke import Stage5Smoke


def test_stage5_smoke_uses_an_isolated_workspace_and_project_name(tmp_path: Path) -> None:
    smoke = Stage5Smoke.create(tmp_path)

    assert smoke.workspace.parent == tmp_path
    assert smoke.workspace.name.startswith("stage5-template-")
    assert smoke.compose_project_name.startswith("codegen_stage5_")
    assert smoke.template == "gh:vladmesh/service-template"
    assert smoke.template_ref == "0.3.0"


def test_stage5_mock_smoke_runs_the_worker_mode_contract(tmp_path: Path) -> None:
    smoke = Stage5Smoke.create(tmp_path)

    smoke.run()
