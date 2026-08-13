from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


def test_production_deploy_provisions_codex_worker_runtime():
    # The codex image is named in the chain the two halves of the worker image
    # release share (publish and pull), not repeated in each of them; the pull half
    # is what carries it into production, so it has to read that chain.
    worker_chain = (ROOT / "infra/scripts/worker-images.sh").read_text()
    pull_script = (ROOT / "infra/scripts/pull-worker-images.sh").read_text()
    deploy_workflow = (ROOT / ".github/workflows/deploy.yml").read_text()
    deploy_runbook = (ROOT / "docs/DEPLOY.md").read_text()

    assert '"worker-base-codex"' in worker_chain
    assert "worker-images.sh" in pull_script
    assert "HOST_CODEX_HOME=${{ secrets.HOST_CODEX_HOME }}" in deploy_workflow
    assert "`HOST_CODEX_HOME`" in deploy_runbook
