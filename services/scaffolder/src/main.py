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

logger = structlog.get_logger()

# Configuration
REDIS_HOST = os.getenv("REDIS_HOST", "redis")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))
API_URL = os.getenv("API_URL", "http://api:8000")
GITHUB_APP_ID = os.getenv("GITHUB_APP_ID")
GITHUB_APP_PRIVATE_KEY_PATH = os.getenv("GITHUB_APP_PRIVATE_KEY_PATH", "/app/keys/github_app.pem")
SERVICE_TEMPLATE_REPO = os.getenv("SERVICE_TEMPLATE_REPO", "gh:vladmesh/service-template")

QUEUE_NAME = "scaffolder:queue"
CONSUMER_GROUP = "scaffolder-workers"
CONSUMER_NAME = f"scaffolder-{os.getpid()}"


async def get_github_token(org: str) -> str:
    """Get GitHub App installation token for the organization."""
    import time

    import jwt

    # Load private key
    with open(GITHUB_APP_PRIVATE_KEY_PATH) as f:
        private_key = f.read()

    # Generate JWT
    now = int(time.time())
    payload = {"iat": now - 60, "exp": now + (10 * 60), "iss": GITHUB_APP_ID}
    jwt_token = jwt.encode(payload, private_key, algorithm="RS256")

    headers = {
        "Authorization": f"Bearer {jwt_token}",
        "Accept": "application/vnd.github+json",
    }

    async with httpx.AsyncClient() as client:
        # Get installation ID for org
        resp = await client.get(
            f"https://api.github.com/orgs/{org}/installation",
            headers=headers,
        )
        resp.raise_for_status()
        installation_id = resp.json()["id"]

        # Get installation token
        resp = await client.post(
            f"https://api.github.com/app/installations/{installation_id}/access_tokens",
            headers=headers,
        )
        resp.raise_for_status()
        return resp.json()["token"]


async def update_project_status(project_id: str, status: str, max_retries: int = 3) -> bool:
    """Update project status via API with retry logic.

    Args:
        project_id: Project ID to update
        status: New status value
        max_retries: Maximum number of retry attempts

    Returns:
        True if update succeeded, False otherwise
    """
    for attempt in range(max_retries):
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.patch(
                    f"{API_URL}/api/projects/{project_id}",
                    json={"status": status},
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
) -> bool:
    """Clone repo, run copier, commit and push.

    Args:
        repo_full_name: Full repo name (org/repo)
        project_name: Project name for copier
        project_id: Project ID for status updates
        modules: Comma-separated list of modules

    Returns:
        True if successful, False otherwise
    """
    org = repo_full_name.split("/")[0]

    try:
        # Get GitHub token
        token = await get_github_token(org)

        with tempfile.TemporaryDirectory() as tmpdir:
            repo_dir = Path(tmpdir) / "repo"

            # 1. Clone repository
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
                "--trust",
                "--defaults",
                "--overwrite",  # Overwrite existing files from init
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

            # 3. Configure git user
            _run_git("config", "user.email", "scaffolder@codegen.local", cwd=repo_dir)
            _run_git("config", "user.name", "Scaffolder Bot", cwd=repo_dir)

            # 4. Commit changes
            logger.info("committing_changes")
            _run_git("add", ".", cwd=repo_dir)

            result = _run_git(
                "commit",
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


async def process_job(job_data: dict) -> None:
    """Process a scaffolding job."""
    repo_full_name = job_data.get("repo_full_name")
    project_name = job_data.get("project_name")
    project_id = job_data.get("project_id")
    modules = job_data.get("modules", "backend")

    if not all([repo_full_name, project_name, project_id]):
        logger.error("invalid_job_data", data=job_data)
        return

    logger.info(
        "processing_scaffold_job",
        repo=repo_full_name,
        project_name=project_name,
        project_id=project_id,
        modules=modules,
    )

    success = await scaffold_project(repo_full_name, project_name, project_id, modules)

    if success:
        await update_project_status(project_id, "scaffolded")
    else:
        await update_project_status(project_id, "scaffold_failed")


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
                        await process_job(job_data)
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
