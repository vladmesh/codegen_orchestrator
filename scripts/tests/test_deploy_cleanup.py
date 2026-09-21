"""Regression coverage for deploy-host cleanup ordering."""

from pathlib import Path

DEPLOY_WORKFLOW = Path(__file__).resolve().parents[2] / ".github/workflows/deploy.yml"


def _cleanup_script() -> str:
    workflow = DEPLOY_WORKFLOW.read_text(encoding="utf-8")
    cleanup = workflow.split("      - name: Cleanup\n", maxsplit=1)[1]
    return cleanup.split("          script: |\n", maxsplit=1)[1]


def test_cleanup_prunes_cache_and_dangling_images_after_worker_cleanup_failure_is_saved():
    script = _cleanup_script()

    worker_cleanup = script.index(
        "--previous-release-record previous-deployed-worker-images.json || cleanup_status=$?"
    )
    build_cache_prune = script.index("docker builder prune --all --force --filter until=24h")
    dangling_image_prune = script.index("docker image prune -f")
    saved_status_exit = script.index('if [ "${cleanup_status}" -ne 0 ]; then')

    assert worker_cleanup < build_cache_prune < dangling_image_prune < saved_status_exit
