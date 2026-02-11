"""Scaffolder service - automatically scaffolds projects with copier.

Listens to scaffolder:queue Redis Stream and runs copier to scaffold
project structure from service-template.
"""

import asyncio
import os
from pathlib import Path
import subprocess
import tempfile

import httpx
import redis.asyncio as redis
import structlog

from shared.clients.github import GitHubAppClient
from shared.contracts.queues.scaffolder import ScaffolderAction, ScaffolderMessage, ScaffolderResult

logger = structlog.get_logger()

# Configuration
REDIS_HOST = os.getenv("REDIS_HOST", "redis")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))
API_URL = os.getenv("API_URL", "http://api:8000")
SERVICE_TEMPLATE_REPO = os.getenv("SERVICE_TEMPLATE_REPO", "gh:vladmesh/service-template")

QUEUE_NAME = "scaffolder:queue"
RESULT_QUEUE_NAME = "scaffolder:results"
CONSUMER_GROUP = "scaffolder-workers"
CONSUMER_NAME = f"scaffolder-{os.getpid()}"

# Shared GitHub client
github_client = GitHubAppClient()


async def get_github_token(org: str) -> str:
    """Get GitHub App installation token for the organization using shared client."""
    return await github_client.get_org_token(org)


async def update_project(
    project_id: str,
    status: str,
    repository_url: str | None = None,
    max_retries: int = 3,
) -> bool:
    """Update project via API with retry logic.

    Args:
        project_id: Project ID to update
        status: New status value
        repository_url: Optional repository URL to set
        max_retries: Maximum number of retry attempts

    Returns:
        True if update succeeded, False otherwise
    """
    payload = {"status": status}
    if repository_url:
        payload["repository_url"] = repository_url

    for attempt in range(max_retries):
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.patch(
                    f"{API_URL}/api/projects/{project_id}",
                    json=payload,
                )
                if resp.is_success:
                    logger.info(
                        "project_status_updated",
                        project_id=project_id,
                        status=status,
                    )
                    return True
                logger.warning(
                    "failed_to_update_project_status",
                    project_id=project_id,
                    status=status,
                    response=resp.text,
                    attempt=attempt + 1,
                )
        except Exception as e:
            logger.warning(
                "update_project_status_error",
                project_id=project_id,
                status=status,
                error=str(e),
                attempt=attempt + 1,
            )

        if attempt < max_retries - 1:
            wait_time = 2**attempt  # Exponential backoff: 1s, 2s, 4s
            await asyncio.sleep(wait_time)

    logger.error(
        "update_project_status_failed_all_retries",
        project_id=project_id,
        status=status,
        max_retries=max_retries,
    )
    return False


def _run_git(
    *args: str, cwd: Path | None = None, check_result: bool = True
) -> subprocess.CompletedProcess[str]:
    """Run git command with full path for security."""
    import shutil

    git_path = shutil.which("git")
    if not git_path:
        raise RuntimeError("git not found in PATH")

    result = subprocess.run(
        [git_path, *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )
    return result


async def scaffold_project(
    repo_full_name: str,
    project_name: str,
    project_id: str,
    modules: str,
    task_description: str = "",
) -> bool:
    """Create repo, run copier, commit and push.

    Args:
        repo_full_name: Full repo name (org/repo)
        project_name: Project name for copier
        project_id: Project ID for status updates
        modules: Comma-separated list of modules
        task_description: Detailed task description for TASK.md

    Returns:
        True if successful, False otherwise
    """
    org, repo_name = repo_full_name.split("/")

    try:
        # Step 1: Create repository if it doesn't exist
        logger.info("creating_repo", org=org, repo=repo_name)
        try:
            await github_client.create_repo(
                org=org,
                name=repo_name,
                description=f"Project: {project_name}",
                private=True,
            )
            logger.info("repo_created", repo=repo_full_name)
        except Exception as e:
            # Repo might already exist (422 Unprocessable Entity)
            error_str = str(e).lower()
            if "already exists" in error_str or "422" in error_str:
                logger.info("repo_already_exists", repo=repo_full_name)
            else:
                logger.error("repo_creation_failed", error=str(e))
                raise

        # Get GitHub token for git operations
        token = await get_github_token(org)

        with tempfile.TemporaryDirectory() as tmpdir:
            repo_dir = Path(tmpdir) / "repo"

            # Step 2: Clone repository
            logger.info("cloning_repo", repo=repo_full_name)
            clone_url = f"https://x-access-token:{token}@github.com/{repo_full_name}.git"

            result = _run_git("clone", clone_url, str(repo_dir))
            if result.returncode != 0:
                logger.error("git_clone_failed", error=result.stderr)
                return False

            # 2. Run copier
            # Sanitize project_name: lowercase, hyphens only, no underscores/spaces
            import re

            sanitized_name = project_name.lower().replace("_", "-").replace(" ", "-")
            sanitized_name = re.sub(r"[^a-z0-9-]", "", sanitized_name)  # Remove invalid chars
            sanitized_name = re.sub(r"-+", "-", sanitized_name).strip(
                "-"
            )  # Collapse multiple hyphens
            if not sanitized_name or not sanitized_name[0].isalpha():
                sanitized_name = "project-" + sanitized_name  # Ensure starts with letter

            logger.info("running_copier", project_name=sanitized_name, modules=modules)

            import shutil

            copier_path = shutil.which("copier")
            if not copier_path:
                logger.error("copier_not_found")
                return False

            copier_cmd = [
                copier_path,
                "copy",
                SERVICE_TEMPLATE_REPO,
                str(repo_dir),
                "--data",
                f"project_name={sanitized_name}",
                "--data",
                f"modules={modules}",
                "--data",
                f"task_description={task_description}",
                "--trust",
                "--defaults",
                "--overwrite",  # Overwrite existing files from init
                "--vcs-ref=HEAD",  # Always use latest commit, not cached tag
            ]

            result = subprocess.run(
                copier_cmd,
                capture_output=True,
                text=True,
                check=False,
                env={**os.environ, "HOME": tmpdir},  # copier needs HOME
            )
            if result.returncode != 0:
                logger.error("copier_failed", error=result.stderr, stdout=result.stdout)
                return False

            # 3. Configure git user and disable hooks
            _run_git("config", "user.email", "scaffolder@codegen.local", cwd=repo_dir)
            _run_git("config", "user.name", "Scaffolder Bot", cwd=repo_dir)
            _run_git("config", "core.hooksPath", "/dev/null", cwd=repo_dir)  # Disable all hooks

            # 4. Commit changes
            logger.info("committing_changes")
            _run_git("add", ".", cwd=repo_dir)

            result = _run_git(
                "commit",
                "--no-verify",  # Skip pre-commit hooks (no make in container)
                "-m",
                f"feat: scaffold {project_name} with modules: {modules}",
                cwd=repo_dir,
            )
            if result.returncode != 0 and "nothing to commit" not in result.stdout:
                logger.error("git_commit_failed", error=result.stderr)
                return False

            # 5. Push
            logger.info("pushing_changes")
            result = _run_git("push", "origin", "main", cwd=repo_dir)
            if result.returncode != 0:
                logger.error("git_push_failed", error=result.stderr)
                return False

            logger.info("scaffold_complete", repo=repo_full_name, modules=modules)
            return True

    except Exception as e:
        logger.exception("scaffold_error", error=str(e))
        return False


async def update_project_copier(
    repo_full_name: str,
    project_id: str,
) -> bool:
    """Clone existing repo, run copier update, commit and push.

    Args:
        repo_full_name: Full repo name (org/repo)
        project_id: Project ID for logging

    Returns:
        True if successful, False otherwise
    """
    org, repo_name = repo_full_name.split("/")

    try:
        token = await get_github_token(org)

        with tempfile.TemporaryDirectory() as tmpdir:
            repo_dir = Path(tmpdir) / "repo"

            # Step 1: Clone repository
            logger.info("update_cloning_repo", repo=repo_full_name)
            clone_url = f"https://x-access-token:{token}@github.com/{repo_full_name}.git"

            result = _run_git("clone", clone_url, str(repo_dir))
            if result.returncode != 0:
                logger.error("update_git_clone_failed", error=result.stderr)
                return False

            # Step 2: Run copier update
            import shutil

            copier_path = shutil.which("copier")
            if not copier_path:
                logger.error("copier_not_found")
                return False

            logger.info("running_copier_update", repo=repo_full_name)
            copier_cmd = [
                copier_path,
                "update",
                "--defaults",
                "--trust",
            ]

            result = subprocess.run(
                copier_cmd,
                cwd=repo_dir,
                capture_output=True,
                text=True,
                check=False,
                env={**os.environ, "HOME": tmpdir},
            )
            if result.returncode != 0:
                logger.error(
                    "copier_update_failed",
                    error=result.stderr,
                    stdout=result.stdout,
                )
                return False

            # Step 3: Configure git user and disable hooks
            _run_git("config", "user.email", "scaffolder@codegen.local", cwd=repo_dir)
            _run_git("config", "user.name", "Scaffolder Bot", cwd=repo_dir)
            _run_git("config", "core.hooksPath", "/dev/null", cwd=repo_dir)

            # Step 4: Check if there are changes
            status_result = _run_git("status", "--porcelain", cwd=repo_dir)
            if not status_result.stdout.strip():
                logger.info("copier_update_no_changes", repo=repo_full_name)
                return True  # No changes needed, still success

            # Step 5: Commit changes
            logger.info("update_committing_changes", repo=repo_full_name)
            _run_git("add", ".", cwd=repo_dir)

            result = _run_git(
                "commit",
                "--no-verify",
                "-m",
                "chore: update framework via copier update",
                cwd=repo_dir,
            )
            if result.returncode != 0 and "nothing to commit" not in result.stdout:
                logger.error("update_git_commit_failed", error=result.stderr)
                return False

            # Step 6: Push
            logger.info("update_pushing_changes", repo=repo_full_name)
            result = _run_git("push", "origin", "main", cwd=repo_dir)
            if result.returncode != 0:
                logger.error("update_git_push_failed", error=result.stderr)
                return False

            logger.info("copier_update_complete", repo=repo_full_name)
            return True

    except Exception as e:
        logger.exception("copier_update_error", error=str(e))
        return False


async def process_job(job_data: dict, redis_client: redis.Redis) -> None:
    """Process a scaffolding job using ScaffolderMessage DTO."""
    # Parse with Pydantic DTO for validation
    try:
        message = ScaffolderMessage.model_validate(job_data)
    except Exception as e:
        logger.error("invalid_job_data", data=job_data, error=str(e))
        return

    logger.info(
        "processing_scaffold_job",
        repo=message.repo_full_name,
        project_name=message.project_name,
        project_id=message.project_id,
        modules=[m.value for m in message.modules],
    )

    # Route by action
    if message.action == ScaffolderAction.UPDATE:
        success = await update_project_copier(
            message.repo_full_name,
            message.project_id,
        )
    else:
        # Convert list of enums to comma-separated string for copier
        modules_str = ",".join(m.value for m in message.modules) if message.modules else "backend"
        success = await scaffold_project(
            message.repo_full_name,
            message.project_name,
            message.project_id,
            modules_str,
            task_description=message.task_description,
        )

    # Publish ScaffolderResult to results queue
    result = ScaffolderResult(
        request_id=message.request_id,
        project_id=message.project_id,
        repo_url=f"https://github.com/{message.repo_full_name}",
        status="success" if success else "failed",
        error=None if success else "Scaffolding failed - check logs",
    )
    await redis_client.xadd(RESULT_QUEUE_NAME, {"data": result.model_dump_json()})
    logger.info(
        "scaffold_result_published",
        project_id=message.project_id,
        status=result.status,
        queue=RESULT_QUEUE_NAME,
    )

    # Also update project via API
    repo_url = f"https://github.com/{message.repo_full_name}"
    if success:
        await update_project(message.project_id, "scaffolded", repository_url=repo_url)
    else:
        await update_project(message.project_id, "scaffold_failed")


async def ensure_consumer_group(redis_client: redis.Redis) -> None:
    """Ensure consumer group exists."""
    try:
        await redis_client.xgroup_create(
            QUEUE_NAME,
            CONSUMER_GROUP,
            id="0",
            mkstream=True,
        )
        logger.info("consumer_group_created", group=CONSUMER_GROUP)
    except redis.ResponseError as e:
        if "BUSYGROUP" not in str(e):
            raise


async def main() -> None:
    """Main loop - consume jobs from Redis Stream."""
    logger.info("scaffolder_starting", queue=QUEUE_NAME)

    redis_client = redis.Redis(
        host=REDIS_HOST,
        port=REDIS_PORT,
        decode_responses=True,
    )

    await ensure_consumer_group(redis_client)

    logger.info("scaffolder_ready", consumer=CONSUMER_NAME)

    while True:
        try:
            # Read from stream with blocking
            messages = await redis_client.xreadgroup(
                CONSUMER_GROUP,
                CONSUMER_NAME,
                {QUEUE_NAME: ">"},
                count=1,
                block=5000,  # 5 second timeout
            )

            if not messages:
                continue

            for _stream, stream_messages in messages:
                for message_id, data in stream_messages:
                    try:
                        # Handle RedisStreamClient JSON wrapper format
                        if "data" in data and isinstance(data["data"], str):
                            import json as json_lib

                            job_data = json_lib.loads(data["data"])
                        else:
                            job_data = data
                        await process_job(job_data, redis_client)
                        # Acknowledge message
                        await redis_client.xack(QUEUE_NAME, CONSUMER_GROUP, message_id)
                    except Exception as e:
                        logger.exception(
                            "job_processing_error", message_id=message_id, error=str(e)
                        )

        except Exception as e:
            logger.exception("main_loop_error", error=str(e))
            await asyncio.sleep(5)


if __name__ == "__main__":
    asyncio.run(main())
