import asyncio
import json
import os
import subprocess
from typing import Any

import structlog

from shared.redis.client import RedisStreamClient

from .config import WorkerWrapperConfig

logger = structlog.get_logger(__name__)

WORKSPACE_DIR = "/workspace"
TASK_MD_PATH = "/home/worker/TASK.md"


class WorkerWrapper:
    """
    Wraps a worker agent process, handling Redis Stream communication
    and lifecycle management.
    """

    def __init__(self, config: WorkerWrapperConfig, redis_client: RedisStreamClient | None = None):
        self.config = config
        if redis_client:
            self.redis = redis_client
            self._owns_redis = False
        else:
            self.redis = RedisStreamClient(redis_url=config.redis_url)
            self._owns_redis = True
        self._running = False
        self._task: asyncio.Task | None = None

    async def run(self):
        """Main loop: connect, consume, execute, publish."""
        self._running = True
        logger.info("worker_wrapper_starting", config=self.config.model_dump())

        await self.redis.connect()
        # Ensure group exists
        await self.redis.ensure_consumer_group(self.config.input_stream, self.config.consumer_group)

        try:
            async for message in self.redis.consume(
                stream=self.config.input_stream,
                group=self.config.consumer_group,
                consumer=self.config.consumer_name,
                block_ms=self.config.poll_interval_ms,
            ):
                if not self._running:
                    break

                if message is None:
                    # Timeout/No message, continue loop
                    continue

                await self.process_message(message)

        except asyncio.CancelledError:
            logger.info("worker_wrapper_cancelled")
        except Exception as e:
            logger.exception("worker_wrapper_crashed", error=str(e))
            raise
        finally:
            if self._owns_redis:
                await self.redis.close()
                logger.info("worker_wrapper_stopped")

    async def process_message(self, message):
        """Process a single task message."""
        msg_id = message.message_id
        data = message.data

        logger.info("processing_task", msg_id=msg_id)

        # Persist task context for crash recovery (Gap B)
        # We save task_id/request_id so DockerEventsListener can read them if container dies
        context_update = {}
        if "task_id" in data:
            context_update["task_id"] = data["task_id"]
        if "request_id" in data:
            context_update["request_id"] = data["request_id"]

        if context_update:
            # Access raw redis client to use hset
            await self.redis.redis.hset(
                f"worker:status:{self.config.consumer_name}", mapping=context_update
            )

        # 1. Lifecycle: Started
        await self.publish_lifecycle("started", msg_id)

        # 2. Pre-turn: pull latest changes and update TASK.md
        await self._git_pull()

        prompt = data.get("prompt")
        if prompt:
            self._write_task_md(prompt)

        # 3. Pre-flight: verify workspace has project files (not just README)
        workspace_ok, workspace_detail = self._check_workspace_ready()
        if not workspace_ok:
            logger.error(
                "workspace_preflight_failed",
                detail=workspace_detail,
                hint="Workspace appears empty — scaffold phase likely failed or was skipped. "
                "Refusing to launch agent to avoid wasting credits.",
            )
            result = None
            error = f"Workspace pre-flight failed: {workspace_detail}"
            status = "failed"
            await self.redis.publish(
                self.config.output_stream,
                {"status": "failed", "error": error},
            )
            await self.publish_lifecycle(status, msg_id, result=result, error=error)
            return
        logger.info("workspace_preflight_passed", detail=workspace_detail)

        # 4. Execute
        try:
            result = await self.execute_agent(data)
            status = "completed"
            error = None
        except Exception as e:
            logger.error("execution_failed", error=str(e))
            result = None
            error = str(e)
            status = "failed"

        # 4. Publish Result to output stream (success or error)
        if result:
            await self.redis.publish(self.config.output_stream, result)
        elif error:
            await self.redis.publish(
                self.config.output_stream,
                {"status": "failed", "error": error},
            )

        # 5. Lifecycle: Completed/Failed
        await self.publish_lifecycle(status, msg_id, result=result, error=error)

    def _check_workspace_ready(self) -> tuple[bool, str]:
        """Check that workspace has real project files, not just an empty repo.

        Returns (ok, detail) — detail is a human-readable summary for logging.
        Only checks when WORKSPACE_DIR exists (inside a container). Outside
        containers (tests), skips gracefully.
        """
        if not os.path.isdir(WORKSPACE_DIR):
            return True, "workspace dir does not exist (not in container, skipping check)"

        marker = os.path.join(WORKSPACE_DIR, ".copier-answers.yml")
        makefile = os.path.join(WORKSPACE_DIR, "Makefile")
        services_dir = os.path.join(WORKSPACE_DIR, "services")

        has_copier = os.path.isfile(marker)
        has_makefile = os.path.isfile(makefile)
        has_services = os.path.isdir(services_dir)

        # List top-level entries for diagnostics
        try:
            entries = sorted(os.listdir(WORKSPACE_DIR))
            visible = [
                e
                for e in entries
                if not e.startswith(".") or e in (".copier-answers.yml", ".github")
            ]
        except OSError:
            return False, "workspace directory is unreadable"

        detail = (
            f"files={visible}, "
            f"copier_marker={has_copier}, "
            f"makefile={has_makefile}, "
            f"services_dir={has_services}"
        )

        # Scaffold produces .copier-answers.yml — its absence means scaffold didn't run
        if not has_copier:
            return False, f"missing .copier-answers.yml (scaffold not run). {detail}"

        return True, detail

    async def _git_pull(self):
        """Pull latest changes before next agent turn."""
        result = subprocess.run(
            ["/usr/bin/git", "pull", "--rebase=false"],  # noqa: S603
            cwd=WORKSPACE_DIR,
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode != 0:
            logger.warning("git_pull_failed", stderr=result.stderr)

    def _write_task_md(self, prompt: str):
        """Write prompt to TASK.md so agent sees the updated task."""
        try:
            with open(TASK_MD_PATH, "w") as f:
                f.write(prompt)
            logger.info("task_md_updated", path=TASK_MD_PATH)
        except OSError as e:
            logger.warning("task_md_write_failed", error=str(e))

    def _get_git_head(self) -> str | None:
        """Get current HEAD SHA in workspace. Returns None if not a git repo or on error."""
        try:
            result = subprocess.run(
                ["/usr/bin/git", "rev-parse", "HEAD"],  # noqa: S603
                cwd=WORKSPACE_DIR,
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode != 0:
                logger.warning("git_head_not_available", stderr=result.stderr.strip())
                return None
            return result.stdout.strip()
        except Exception as e:
            logger.warning("git_head_failed", error=str(e))
            return None

    def _extract_git_commit_sha(self, initial_head: str | None) -> str | None:
        """Compare current HEAD with initial_head to detect new commits.

        Returns new SHA only if HEAD changed (agent made a commit).
        All failures return None (warning log, no crash).
        """
        try:
            result = subprocess.run(
                ["/usr/bin/git", "log", "-1", "--format=%H"],  # noqa: S603
                cwd=WORKSPACE_DIR,
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode != 0:
                logger.warning("git_log_failed", stderr=result.stderr.strip())
                return None
            current_head = result.stdout.strip()
            if not current_head:
                return None
            # If initial_head is None (empty repo before agent), any commit is new
            if initial_head is None:
                return current_head
            # HEAD changed → new commit detected
            if current_head != initial_head:
                return current_head
            return None
        except Exception as e:
            logger.warning("git_commit_sha_extraction_failed", error=str(e))
            return None

    async def execute_agent(self, data: dict[str, Any]) -> dict[str, Any] | None:
        """
        Execute the agent using the configured runner and parsing logic.
        """
        # 1. Get Session
        # We need raw redis client for session manager
        # RedisStreamClient exposes .redis property which is redis.Redis
        from .session import SessionManager

        session_manager = SessionManager(
            redis=self.redis.redis, worker_id=self.config.consumer_name
        )

        # Gap solution: Claude CLI manages its own session IDs and doesn't accept random ones.
        # So for Claude, we don't create a new random ID.
        create_new_session = self.config.agent_type != "claude"
        session_id = await session_manager.get_or_create_session(create_new=create_new_session)

        # 2. Select Runner
        from .runners.claude import ClaudeRunner
        from .runners.factory import FactoryRunner

        if self.config.agent_type == "claude":
            runner = ClaudeRunner(session_id=session_id)
        elif self.config.agent_type == "factory":
            runner = FactoryRunner()  # Factory runner might not need session or handled differently
        else:
            raise ValueError(f"Unknown agent type: {self.config.agent_type}")

        # 3. Build Command
        # PO workers use 'content', Developer workers use 'prompt' (DeveloperWorkerInput)
        prompt = data.get("content") or data.get("prompt", "")
        if not prompt:
            raise ValueError("Task data missing 'content' or 'prompt'")

        cmd = runner.build_command(prompt=prompt)
        logger.info("executing_agent_command", cmd=cmd)

        # 3.5. Snapshot initial HEAD before agent runs
        initial_head = self._get_git_head()

        # 4. Execute Subprocess
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(), timeout=self.config.subprocess_timeout_seconds
            )
        except TimeoutError:
            logger.error(
                "agent_process_timed_out",
                timeout=self.config.subprocess_timeout_seconds,
            )
            try:
                proc.kill()
                await proc.wait()
            except ProcessLookupError:
                pass
            raise RuntimeError(
                f"Agent process timed out after {self.config.subprocess_timeout_seconds} seconds"
            ) from None
        stdout = stdout_bytes.decode().strip()
        stderr = stderr_bytes.decode().strip()

        if proc.returncode != 0:
            logger.error(
                "agent_process_failed", stderr=stderr, stdout=stdout, exit_code=proc.returncode
            )
            raise RuntimeError(f"Agent process failed with code {proc.returncode}: {stderr}")

        # 5. Capture session_id from Claude CLI JSON output
        # Claude CLI with --output-format json returns session_id in the response
        if self.config.agent_type == "claude" and not session_id:
            captured_session_id = self._extract_session_id_from_output(stdout)
            if captured_session_id:
                logger.info("captured_claude_session_from_output", session_id=captured_session_id)
                await session_manager.update_session(captured_session_id)

        # 6. Parse Result
        from .result_parser import ResultParseError, ResultParser

        try:
            parsed = ResultParser.parse(stdout)
            if parsed is None:
                # No <result> tags — try extracting plain text from Claude CLI JSON
                content = ResultParser.extract_text(stdout)
                if content:
                    result = {"content": content, "status": "success"}
                else:
                    logger.warning("no_result_tags_found", stdout=stdout[:500])
                    result = {"raw_output": stdout, "status": "no_structured_result"}
            else:
                result = parsed
        except ResultParseError as e:
            logger.error("result_parsing_failed", error=str(e), stdout=stdout)
            raise

        # 7. Enrich with git SHA — git is the authoritative source
        git_sha = self._extract_git_commit_sha(initial_head)
        if git_sha:
            logger.info("git_commit_sha_detected", sha=git_sha, source="git")
            result["commit_sha"] = git_sha

        return result

    def _extract_session_id_from_output(self, stdout: str) -> str | None:
        """
        Extract session_id from Claude CLI JSON output.

        Claude CLI with --output-format json returns:
        {
            "type": "result",
            "session_id": "uuid-here",
            ...
        }
        """
        try:
            # stdout may contain multiple JSON objects (streaming), find the result one
            # Try parsing the whole output first
            data = json.loads(stdout)
            if isinstance(data, dict) and "session_id" in data:
                return data["session_id"]
        except json.JSONDecodeError:
            # Try to find JSON objects line by line
            for line in stdout.split("\n"):
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                    if isinstance(data, dict) and "session_id" in data:
                        return data["session_id"]
                except json.JSONDecodeError:
                    continue
        except Exception as e:
            logger.warning("failed_to_extract_session_id", error=str(e))

        return None

    async def publish_lifecycle(
        self, status: str, ref_msg_id: str, result: dict = None, error: str = None
    ):
        """Publish lifecycle event."""
        # Use shared contract
        from shared.contracts.queues.worker_lifecycle import WorkerLifecycleEvent

        event = WorkerLifecycleEvent(
            worker_id=self.config.consumer_name,  # using consumer name as worker_id
            event=status,
            result=result,
            error=error,
        )

        await self.redis.publish_message("worker:lifecycle", event)
