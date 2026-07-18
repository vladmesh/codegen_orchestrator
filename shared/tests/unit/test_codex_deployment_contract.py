from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


def test_production_deploy_provisions_codex_worker_runtime():
    pull_script = (ROOT / "infra/scripts/pull-worker-images.sh").read_text()
    deploy_workflow = (ROOT / ".github/workflows/deploy.yml").read_text()
    deploy_runbook = (ROOT / "docs/DEPLOY.md").read_text()

    assert '"worker-base-codex"' in pull_script
    assert "HOST_CODEX_HOME=${{ secrets.HOST_CODEX_HOME }}" in deploy_workflow
    assert "`HOST_CODEX_HOME`" in deploy_runbook
