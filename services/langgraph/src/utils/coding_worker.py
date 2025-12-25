"""Coding Worker Spawner.

Utility for spawning Sysbox containers to run AI coding agents.
"""

import asyncio
from dataclasses import dataclass
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

# Path to AGENTS.md template
AGENTS_TEMPLATE_PATH = (
    Path(__file__).parent.parent.parent.parent / "coding-worker" / "templates" / "AGENTS.md"
)


@dataclass
class WorkerResult:
    """Result from a coding worker execution."""

    success: bool
    exit_code: int
    output: str
    commit_sha: str | None = None


async def spawn_coding_worker(
    repo: str,
    github_token: str,
    task_content: str,
    task_title: str = "AI generated changes",
    model: str = "claude-sonnet-4-5-20250929",
    agents_content: str | None = None,
    timeout_seconds: int = 600,
) -> WorkerResult:
    """Spawn a Sysbox container to run AI coding task.

    Args:
        repo: Repository in org/name format
        github_token: GitHub token for clone/push
        task_content: The task description (will be written to TASK.md)
        task_title: Title for the commit message
        model: Factory.ai model to use
        agents_content: Custom AGENTS.md content (uses template if not provided)
        timeout_seconds: Maximum time to wait for worker completion

    Returns:
        WorkerResult with execution details
    """
    factory_api_key = os.getenv("FACTORY_AI_API_KEY")
    if not factory_api_key:
        raise RuntimeError("FACTORY_AI_API_KEY environment variable is not set")

    # Load default AGENTS.md template if not provided
    if agents_content is None and AGENTS_TEMPLATE_PATH.exists():
        agents_content = AGENTS_TEMPLATE_PATH.read_text()

    # Build docker run command
    # Note: Uses sysbox-runc runtime for Docker-in-Docker capabilities
    cmd = [
        "docker",
        "run",
        "--rm",
        "--runtime=sysbox-runc",
        "-e",
        f"GITHUB_TOKEN={github_token}",
        "-e",
        f"FACTORY_API_KEY={factory_api_key}",
        "-e",
        f"REPO={repo}",
        "-e",
        f"TASK_CONTENT={task_content}",
        "-e",
        f"TASK_TITLE={task_title}",
        "-e",
        f"MODEL={model}",
    ]

    if agents_content:
        cmd.extend(["-e", f"AGENTS_CONTENT={agents_content}"])

    cmd.append("coding-worker:latest")

    logger.info(f"Spawning coding worker for repo: {repo}")
    logger.debug(f"Task: {task_content[:200]}...")

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )

        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout_seconds)
            output = stdout.decode() if stdout else ""
        except TimeoutError:
            proc.kill()
            await proc.wait()
            return WorkerResult(
                success=False,
                exit_code=-1,
                output=f"Worker timed out after {timeout_seconds} seconds",
            )

        # Parse output for commit SHA
        commit_sha = None
        for line in output.split("\n"):
            if line.startswith("Pushed commit:"):
                commit_sha = line.split(":")[-1].strip()
                break

        success = proc.returncode == 0

        logger.info(f"Worker completed: success={success}, exit_code={proc.returncode}")
        if commit_sha:
            logger.info(f"Commit SHA: {commit_sha}")

        return WorkerResult(
            success=success,
            exit_code=proc.returncode or 0,
            output=output,
            commit_sha=commit_sha,
        )

    except Exception as e:
        logger.exception(f"Failed to spawn coding worker: {e}")
        return WorkerResult(
            success=False,
            exit_code=-1,
            output=str(e),
        )


async def spawn_coding_worker_simple(
    repo: str,
    github_token: str,
    task_content: str,
    **kwargs,
) -> str:
    """Simplified wrapper that returns just the output for LangGraph tools.

    Returns:
        Human-readable result string
    """
    result = await spawn_coding_worker(repo, github_token, task_content, **kwargs)

    if result.success:
        if result.commit_sha:
            return f"✅ Task completed successfully. Commit: {result.commit_sha}"
        else:
            return "✅ Task completed successfully. No changes were made."
    else:
        output_tail = result.output[-1000:]
        return f"❌ Task failed with exit code {result.exit_code}.\n\nOutput:\n{output_tail}"
