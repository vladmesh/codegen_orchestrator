import asyncio
import json
import os
import subprocess
from typing import Any

import structlog

from shared.redis.client import RedisStreamClient

from .config import WorkerWrapperConfig
from .http_server import ResultHttpServer

logger = structlog.get_logger(__name__)

WORKSPACE_DIR = "/workspace"
TASK_MD_PATH = "/workspace/TASK.md"
STORY_DIR = "/workspace/.story"
OLD_TASKS_DIR = "/workspace/.story/old_tasks"


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
        self._http_server: ResultHttpServer | None = None
        self._result_event: asyncio.Event | None = None
        self._agent_stdout_tail: str | None = None

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

        # 2. Pre-turn setup
        await self._prepare_workspace(data)

        # 3. Pre-flight: verify workspace has project files (not just README)
        workspace_ok, workspace_detail = self._check_workspace_ready()
        if not workspace_ok:
            logger.error(
                "workspace_preflight_failed",
                detail=workspace_detail,
                hint="Workspace appears empty — scaffold phase likely failed or was skipped. "
                "Refusing to launch agent to avoid wasting credits.",
            )
            error = f"Workspace pre-flight failed: {workspace_detail}"
            status = "failed"
            await self.redis.publish(
                self.config.output_stream,
                {"status": "failed", "error": error},
            )
            await self.publish_lifecycle(status, msg_id, error=error)
            return
        logger.info("workspace_preflight_passed", detail=workspace_detail)

        # 3b. Fix venv shebangs
        self._fix_venv_shebangs()

        # 3c. Inject Makefile overrides for compose proxy
        self._inject_makefile_overrides()

        # 4. Start HTTP result server + execute agent
        self._result_event = asyncio.Event()
        self._buffered_result: dict | None = None

        async def _buffer_http_result(redis_data: dict) -> None:
            self._buffered_result = redis_data

        self._http_server = ResultHttpServer(
            worker_id=self.config.consumer_name,
            publish_callback=_buffer_http_result,
            result_event=self._result_event,
            host="127.0.0.1",
            port=self.config.http_server_port,
        )

        try:
            await self._http_server.start()

            try:
                await self.execute_agent(data)
                status = "completed"
                error = None
            except Exception as e:
                logger.error("execution_failed", error=str(e))
                error = str(e)
                status = "failed"

            # 5. Collect report and archive task (before publishing result)
            report = self._read_worker_report()
            self._archive_task(data, report)

            # 6. Publish result
            status, error = await self._publish_result(data, error, status, report)
        finally:
            await self._http_server.stop()
            self._http_server = None

        # 7. Lifecycle: Completed/Failed
        await self.publish_lifecycle(status, msg_id, error=error)

    async def _publish_result(
        self, data: dict, error: str | None, status: str, report: str | None
    ) -> tuple[str, str | None]:
        """Publish task result to Redis, handling HTTP results, errors, and auto-resume.

        Returns (status, error) — potentially updated if watchdog triggers.
        """
        stdout_tail = self._agent_stdout_tail

        if self._result_event.is_set() and self._buffered_result is not None:
            logger.info("result_received_via_http", worker_id=self.config.consumer_name)
            if report:
                self._buffered_result["worker_report"] = report
            if stdout_tail:
                self._buffered_result["agent_stdout_tail"] = stdout_tail
            await self.redis.publish(self.config.output_stream, self._buffered_result)
            return status, error

        if error:
            result_data: dict[str, Any] = {"status": "failed", "error": error}
            if stdout_tail:
                result_data["agent_stdout_tail"] = stdout_tail
            await self.redis.publish(self.config.output_stream, result_data)
            return status, error

        # Watchdog: agent exited without reporting via HTTP
        # Attempt one auto-resume for Claude agents before failing
        if self.config.agent_type == "claude":
            resumed = await self._attempt_auto_resume(data)
            if resumed and self._result_event.is_set() and self._buffered_result is not None:
                logger.info("result_received_after_resume", worker_id=self.config.consumer_name)
                stdout_tail = self._agent_stdout_tail
                if report:
                    self._buffered_result["worker_report"] = report
                if stdout_tail:
                    self._buffered_result["agent_stdout_tail"] = stdout_tail
                await self.redis.publish(self.config.output_stream, self._buffered_result)
                return status, error

        logger.warning("agent_exited_without_result", worker_id=self.config.consumer_name)
        error = "Agent exited without reporting result"
        status = "failed"
        stdout_tail = self._agent_stdout_tail
        result_data = {"status": "failed", "error": error}
        if stdout_tail:
            result_data["agent_stdout_tail"] = stdout_tail
        await self.redis.publish(self.config.output_stream, result_data)
        return status, error

    async def _prepare_workspace(self, data: dict) -> None:
        """Pre-turn setup: pull, update TASK.md/STORY.md, clear session."""
        await self._git_pull()

        prompt = data.get("prompt")
        if prompt:
            self._write_task_md(prompt)

        story_md = data.get("story_md")
        if story_md:
            self._write_story_md(story_md)

        if data.get("clear_session"):
            from .session import SessionManager

            session_manager = SessionManager(
                redis=self.redis.redis, worker_id=self.config.consumer_name
            )
            await session_manager.clear_session()
            logger.info("session_cleared_for_fresh_start")

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

    @staticmethod
    def _read_shebang(path: str) -> str | None:
        """Read shebang line from a file, or None if not a text script."""
        try:
            with open(path, "rb") as f:
                if f.read(2) != b"#!":
                    return None
            with open(path) as f:
                return f.readline().rstrip("\n")
        except (OSError, UnicodeDecodeError):
            return None

    def _detect_scaffold_prefix(self) -> str | None:
        """Find the scaffold-time path prefix by inspecting venv shebangs.

        Returns the prefix string (e.g. "/data/workspaces/repo-xxx/")
        that should be replaced with WORKSPACE_DIR + "/", or None if
        shebangs already point to the correct path.
        """
        import glob

        shebang_len = len("#!")
        for venv_bin in glob.glob(os.path.join(WORKSPACE_DIR, "**/.venv/bin"), recursive=True):
            for entry in os.scandir(venv_bin):
                if not entry.is_file():
                    continue
                first_line = self._read_shebang(entry.path)
                if not first_line:
                    continue

                shebang_path = first_line[shebang_len:]
                if shebang_path.startswith(WORKSPACE_DIR + "/"):
                    continue  # already correct

                # Find where the workspace-relative path starts in the shebang
                rel_venv_bin = os.path.relpath(venv_bin, WORKSPACE_DIR)
                idx = first_line.find(rel_venv_bin)
                if idx > shebang_len:
                    return first_line[shebang_len:idx]
        return None

    def _fix_venv_shebangs(self):
        """Fix venv shebang paths that don't match the container mount point.

        Scaffolder creates venvs at /data/workspaces/<repo_id>/, but workers
        mount the same directory at /workspace. Detects the original scaffold
        prefix and rewrites all venv script shebangs. Runs once per workspace.
        """
        sentinel = os.path.join(WORKSPACE_DIR, ".shebangs_fixed")
        if os.path.exists(sentinel):
            return

        import glob
        import re

        scaffold_prefix = self._detect_scaffold_prefix()
        if not scaffold_prefix:
            self._touch(sentinel)
            return

        logger.info("fixing_venv_shebangs", scaffold_prefix=scaffold_prefix)

        fixed_count = 0
        escaped_prefix = re.escape(scaffold_prefix)
        for venv_bin in glob.glob(os.path.join(WORKSPACE_DIR, "**/.venv/bin"), recursive=True):
            for entry in os.scandir(venv_bin):
                if not entry.is_file():
                    continue
                if not self._read_shebang(entry.path):
                    continue
                try:
                    with open(entry.path) as f:
                        content = f.read()
                except (OSError, UnicodeDecodeError):
                    continue

                new_content = re.sub(
                    f"#!{escaped_prefix}",
                    f"#!{WORKSPACE_DIR}/",
                    content,
                    count=1,
                )
                if new_content != content:
                    with open(entry.path, "w") as f:
                        f.write(new_content)
                    fixed_count += 1

        logger.info("venv_shebangs_fixed", count=fixed_count)
        self._touch(sentinel)

    @staticmethod
    def _touch(path: str):
        """Create an empty file (sentinel marker)."""
        try:
            open(path, "w").close()
        except OSError:
            pass

    def _inject_makefile_overrides(self):
        """Inject Makefile overrides so `make dev-start` uses the compose proxy.

        Workers don't have Docker socket access. The wrapper's HTTP server
        proxies /infra/compose to worker-manager. This override replaces
        the template's `dev-start` (which calls `docker compose` directly)
        with a `curl` to localhost:9090/infra/compose, so that
        `make dev-start svc=db`, `make migrate`, etc. work transparently.
        """
        makefile = os.path.join(WORKSPACE_DIR, "Makefile")
        if not os.path.isfile(makefile):
            return

        override_marker = "# --- orchestrator overrides ---"
        try:
            content = open(makefile).read()
            if override_marker in content:
                return  # already injected

            override = (
                f"\n{override_marker}\n"
                "dev-start:\n"
                "\t@curl -sf -X POST http://localhost:9090/infra/compose "
                """-H 'Content-Type: application/json' """
                """-d '{"args": ["up", "-d", "--wait", "$(svc)"], "cwd": "."}' """
                "| jq -r .stderr || echo 'compose proxy failed'\n"
                "\n"
                "dev-stop:\n"
                "\t@curl -sf -X POST http://localhost:9090/infra/compose "
                """-H 'Content-Type: application/json' """
                """-d '{"args": ["down", "--remove-orphans"], "cwd": "."}' """
                "| jq -r .stderr || echo 'compose proxy failed'\n"
            )
            with open(makefile, "a") as f:
                f.write(override)
            logger.info("makefile_overrides_injected")
        except OSError as e:
            logger.warning("makefile_override_failed", error=str(e))

    async def _git_pull(self):
        """Pull latest changes before next agent turn.

        Pulls from the current branch (story branch or main).
        """
        branch = self._get_git_branch() or "main"
        result = subprocess.run(
            ["/usr/bin/git", "pull", "--rebase=false", "origin", branch],  # noqa: S603
            cwd=WORKSPACE_DIR,
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode != 0:
            logger.warning("git_pull_failed", stderr=result.stderr, branch=branch)

    def _write_task_md(self, prompt: str):
        """Write prompt to TASK.md so agent sees the updated task."""
        try:
            with open(TASK_MD_PATH, "w") as f:
                f.write(prompt)
            logger.info("task_md_updated", path=TASK_MD_PATH)
        except OSError as e:
            logger.warning("task_md_write_failed", error=str(e))

    def _write_story_md(self, content: str):
        """Write .story/STORY.md so the worker has story-level context."""
        story_md_path = os.path.join(STORY_DIR, "STORY.md")
        try:
            os.makedirs(STORY_DIR, exist_ok=True)
            with open(story_md_path, "w") as f:
                f.write(content)
            logger.info("story_md_updated", path=story_md_path)
        except OSError as e:
            logger.warning("story_md_write_failed", error=str(e))

    def _get_git_branch(self) -> str | None:
        """Get current branch name in workspace. Returns None if detached HEAD or error."""
        try:
            result = subprocess.run(
                ["/usr/bin/git", "rev-parse", "--abbrev-ref", "HEAD"],  # noqa: S603
                cwd=WORKSPACE_DIR,
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode != 0:
                return None
            branch = result.stdout.strip()
            # "HEAD" means detached HEAD state
            if branch == "HEAD":
                return None
            return branch
        except Exception as e:
            logger.warning("git_branch_failed", error=str(e))
            return None

    async def execute_agent(self, data: dict[str, Any]) -> None:
        """Execute the agent subprocess.

        Results are reported by the agent via HTTP (localhost:9090).
        This method only manages the subprocess lifecycle and session.
        """
        from .session import SessionManager

        session_manager = SessionManager(
            redis=self.redis.redis, worker_id=self.config.consumer_name
        )

        create_new_session = self.config.agent_type != "claude"
        session_id = await session_manager.get_or_create_session(create_new=create_new_session)

        # Select Runner
        from .runners.claude import ClaudeRunner
        from .runners.factory import FactoryRunner
        from .runners.noop import NoopRunner

        if self.config.agent_type == "claude":
            runner = ClaudeRunner(session_id=session_id)
        elif self.config.agent_type == "factory":
            runner = FactoryRunner()
        elif self.config.agent_type == "noop":
            runner = NoopRunner()
        else:
            raise ValueError(f"Unknown agent type: {self.config.agent_type}")

        prompt = self._resolve_prompt(data)
        cmd = runner.build_command(prompt=prompt)
        logger.info("executing_agent_command", cmd=cmd)

        # Build subprocess env: remove /app from PYTHONPATH.
        # The worker image sets PYTHONPATH=/app so the wrapper itself can
        # import orchestrator's ``shared``.  But the agent subprocess runs
        # inside a scaffolded project whose venvs have their own ``shared``
        # package (installed via .pth editable links).  Keeping /app in
        # PYTHONPATH shadows the project's shared — e.g. the orchestrator's
        # shared has no ``logging`` module, causing ModuleNotFoundError.
        agent_env = os.environ.copy()
        existing = agent_env.get("PYTHONPATH", "")
        cleaned = os.pathsep.join(p for p in existing.split(os.pathsep) if p and p != "/app")
        if cleaned:
            agent_env["PYTHONPATH"] = cleaned
        else:
            agent_env.pop("PYTHONPATH", None)

        # Execute Subprocess
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=agent_env,
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

        # Capture stdout tail for analytics/debugging (last ~10KB)
        max_tail = 10_000
        combined = stdout
        if stderr:
            combined = f"{stdout}\n--- stderr ---\n{stderr}" if stdout else stderr
        self._agent_stdout_tail = combined[-max_tail:] if combined else None

        if proc.returncode != 0:
            logger.error(
                "agent_process_failed", stderr=stderr, stdout=stdout, exit_code=proc.returncode
            )
            raise RuntimeError(f"Agent process failed with code {proc.returncode}: {stderr}")

        # Capture session_id from Claude CLI JSON output
        if self.config.agent_type == "claude" and not session_id:
            captured_session_id = self._extract_session_id_from_output(stdout)
            if captured_session_id:
                logger.info("captured_claude_session_from_output", session_id=captured_session_id)
                await session_manager.update_session(captured_session_id)

    async def _attempt_auto_resume(self, data: dict) -> bool:
        """Attempt one resume of the Claude agent to get it to call /result.

        Returns True if the resume subprocess completed (result may or may not
        have been received via HTTP — caller checks _result_event).
        """
        logger.warning("attempting_auto_resume", worker_id=self.config.consumer_name)

        from .session import SessionManager

        session_manager = SessionManager(
            redis=self.redis.redis, worker_id=self.config.consumer_name
        )
        session_id = await session_manager.get_or_create_session(create_new=False)

        if not session_id:
            logger.warning("auto_resume_no_session", worker_id=self.config.consumer_name)
            return False

        resume_prompt = (
            "You finished without calling the result endpoint. "
            "Call POST http://localhost:9090/result with your result now. "
            'If the task is done: {"success": true, "commit": "<sha>", "summary": "..."}. '
            'If you could not complete it: {"success": false, "reason": "..."}.'
        )

        from .runners.claude import ClaudeRunner

        runner = ClaudeRunner(session_id=session_id)
        cmd = runner.build_command(prompt=resume_prompt)
        logger.info("auto_resume_command", cmd=cmd)

        agent_env = os.environ.copy()
        existing = agent_env.get("PYTHONPATH", "")
        cleaned = os.pathsep.join(p for p in existing.split(os.pathsep) if p and p != "/app")
        if cleaned:
            agent_env["PYTHONPATH"] = cleaned
        else:
            agent_env.pop("PYTHONPATH", None)

        try:
            resume_timeout = min(120, self.config.subprocess_timeout_seconds)
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=agent_env,
            )
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(), timeout=resume_timeout
            )

            # Update stdout tail with resume output
            stdout = stdout_bytes.decode().strip()
            stderr = stderr_bytes.decode().strip()
            max_tail = 10_000
            combined = stdout
            if stderr:
                combined = f"{stdout}\n--- stderr ---\n{stderr}" if stdout else stderr
            if combined:
                prev = self._agent_stdout_tail or ""
                self._agent_stdout_tail = (prev + "\n--- resume ---\n" + combined)[-max_tail:]

            return True
        except TimeoutError:
            logger.error("auto_resume_timed_out", worker_id=self.config.consumer_name)
            try:
                proc.kill()
                await proc.wait()
            except ProcessLookupError:
                pass
            return False
        except Exception:
            logger.exception("auto_resume_failed", worker_id=self.config.consumer_name)
            return False

    def _collect_and_archive(self, data: dict) -> None:
        """Collect worker report and archive task."""
        report = self._read_worker_report()
        self._archive_task(data, report)

    def _resolve_prompt(self, data: dict[str, Any]) -> str:
        """Resolve the effective prompt for the agent.

        PO workers use 'content', Developer workers use 'prompt'.
        For Claude: minimal redirect to TASK.md (full task already written there).
        For other agents: full prompt passed directly.
        """
        raw = data.get("content") or data.get("prompt", "")
        if not raw:
            raise ValueError("Task data missing 'content' or 'prompt'")

        if self.config.agent_type == "claude":
            return "Read TASK.md and AGENTS.md, then complete the task described in TASK.md."
        return raw

    def _read_worker_report(self) -> str | None:
        """Read and delete REPORT.md from workspace.

        The report is saved to the API as a task event — it doesn't belong
        in the git repo. Deleting after read prevents it from being committed
        by the next task and keeps the workspace clean.
        """
        report_path = os.path.join(WORKSPACE_DIR, "REPORT.md")
        if not os.path.isfile(report_path):
            return None
        try:
            with open(report_path) as f:
                content = f.read()
            os.remove(report_path)
            logger.info("worker_report_collected", size=len(content))
            return content
        except OSError as e:
            logger.warning("worker_report_read_failed", error=str(e))
            return None

    def _archive_task(self, data: dict[str, Any], report: str | None) -> None:
        """Archive completed task: merge TASK.md + REPORT.md → .story/old_tasks/.

        Creates .story/old_tasks/{task_id_or_request_id}.md with the full context:
        task description + developer report. Next worker can browse old_tasks/
        for history. Both TASK.md and REPORT.md are cleaned up after archiving.
        """
        if not os.path.isfile(TASK_MD_PATH):
            return

        try:
            with open(TASK_MD_PATH) as f:
                task_content = f.read()
        except OSError:
            return

        if not task_content.strip():
            return

        # Use task_id if available, fall back to request_id
        archive_id = data.get("task_id") or data.get("request_id") or "unknown"

        # Build archive content
        parts = [task_content]
        if report:
            parts.append("\n---\n")
            parts.append(report)

        archive_content = "\n".join(parts)

        try:
            os.makedirs(OLD_TASKS_DIR, exist_ok=True)

            # Ensure .story is gitignored
            gitignore_path = os.path.join(WORKSPACE_DIR, ".gitignore")
            self._ensure_gitignore_entry(gitignore_path, ".story/")

            archive_path = os.path.join(OLD_TASKS_DIR, f"{archive_id}.md")
            with open(archive_path, "w") as f:
                f.write(archive_content)

            logger.info(
                "task_archived",
                archive_path=archive_path,
                task_id=archive_id,
                size=len(archive_content),
            )
        except OSError as e:
            logger.warning("task_archive_failed", error=str(e))

    @staticmethod
    def _ensure_gitignore_entry(gitignore_path: str, entry: str) -> None:
        """Add entry to .gitignore if not already present."""
        try:
            existing = ""
            if os.path.isfile(gitignore_path):
                with open(gitignore_path) as f:
                    existing = f.read()

            if entry not in existing.splitlines():
                with open(gitignore_path, "a") as f:
                    if existing and not existing.endswith("\n"):
                        f.write("\n")
                    f.write(f"{entry}\n")
        except OSError:
            pass  # Best-effort

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

    async def publish_lifecycle(self, status: str, ref_msg_id: str, error: str = None):
        """Publish lifecycle event."""
        from shared.contracts.queues.worker_lifecycle import WorkerLifecycleEvent

        event = WorkerLifecycleEvent(
            worker_id=self.config.consumer_name,
            event=status,
            error=error,
        )

        await self.redis.publish_message("worker:lifecycle", event)
