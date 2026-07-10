"""Scaffold phase: copier + make setup + git push inside worker containers."""

from __future__ import annotations

import base64
from typing import TYPE_CHECKING

import structlog

from .docker_ops import DockerClientWrapper

if TYPE_CHECKING:
    from shared.contracts.queues.worker import ScaffoldConfig

logger = structlog.get_logger()


async def run_scaffold_phase(
    docker: DockerClientWrapper,
    container_id: str,
    scaffold_config: ScaffoldConfig,
    repo: str,
    token: str,
    worker_id: str,
) -> bool:
    """Run copier + make setup + git push inside the worker container.

    Returns:
        True if scaffold succeeded, False otherwise
    """
    logger.info(
        "scaffold_phase_start",
        worker_id=worker_id,
        template=scaffold_config.template_repo,
        project_name=scaffold_config.project_name,
        modules=scaffold_config.modules,
    )

    # Base64-encode task_description to safely pass it through bash
    task_desc_b64 = base64.b64encode(scaffold_config.task_description.encode()).decode()

    scaffold_script = f"""set -e

# Clone the repo (created via GitHub API with auto_init)
cd /workspace
git clone "https://x-access-token:{token}@github.com/{repo}" .tmp-clone
# Move .git into workspace root (copier will overwrite files, not .git)
mv .tmp-clone/.git /workspace/.git
rm -rf .tmp-clone

git config user.email "ai@codegen.local"
git config user.name "Codegen Bot"
git config core.hooksPath /dev/null

# Install copier via uv (cached after first run)
uv tool install copier

# Write task_description to YAML data file (avoids shell escaping issues)
echo -n '{task_desc_b64}' | base64 -d > /tmp/_copier_desc.txt
printf 'task_description: |\\n' > /tmp/copier-data.yml
sed 's/^/  /' /tmp/_copier_desc.txt >> /tmp/copier-data.yml
rm /tmp/_copier_desc.txt

# Run copier to scaffold project
copier copy {scaffold_config.template_repo} /workspace \
    --data "project_name={scaffold_config.project_name}" \
    --data "modules={scaffold_config.modules}" \
    --data-file /tmp/copier-data.yml \
    --defaults --overwrite --vcs-ref=HEAD

# Setup project (install deps, generate code)
cd /workspace
make setup

# Stage, commit, push
git add .
git commit --no-verify -m "feat: scaffold {scaffold_config.project_name} with modules: {scaffold_config.modules}" || true
git push origin main

# Re-enable hooks for the agent
git config core.hooksPath .githooks
"""

    encoded = base64.b64encode(scaffold_script.encode()).decode()
    cmd = f"bash -c 'echo {encoded} | base64 -d | bash'"

    exit_code, output = await docker.exec_in_container(
        container_id,
        cmd,
        timeout=600,
    )

    if exit_code != 0:
        logger.error(
            "scaffold_phase_failed",
            worker_id=worker_id,
            exit_code=exit_code,
            error=output,
        )
        return False

    logger.info("scaffold_phase_complete", worker_id=worker_id, repo=repo)
    return True
