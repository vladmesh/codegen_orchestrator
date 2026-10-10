"""The orchestrator's own order lifecycle, driven around the kit harness's unchanged stages.

Used by `tests/runner/activated_snapshot_proof.py` inside the harness process, where the
scaffolder's `src` package is importable. Every transition is the production owner's:

* the scheduler's `trigger_scaffolds` and `dispatch_todo_tasks` run as one tick each
  (`tests/runner/scheduler_tick.py`, in the scheduler's environment) and append to the real
  scaffold stream of the runner's Redis;
* each entry is read from that stream by the scaffolder's consumer group and handed, as is, to
  the scaffolder's own entrypoint `process_scaffold_job`, then acknowledged as its worker loop
  does, so the full scaffold (Copier, `make setup`, push, readiness record) and every install
  run in their production code.

The one controlled edge is GitHub, declared here: `ControlledGitHub` answers the GitHub App
client the scaffolder uses, and git reaches a local bare repository through a process-scoped
`insteadOf` while the scaffold runs. Creating the repository initialises that bare remote as
GitHub's `auto_init` would; branch protection and auto-merge are recorded and acknowledged.
"""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
from typing import Any

import httpx

#: The scaffolder worker loop's consumer name prefix; one delivery at a time here.
CONSUMER = "scaffolder-runner"


class ControlledGitHub:
    """The scaffolder's GitHub App client, answered by the runner's local bare repository."""

    remote: Path
    calls: list[dict] = []

    def __init__(self) -> None:
        self.remote = type(self).remote

    async def __aenter__(self) -> ControlledGitHub:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None

    def _record(self, call: str, **details: Any) -> None:
        type(self).calls.append({"call": call, **details})

    async def get_org_token(self, org: str) -> str:
        self._record("get_org_token", org=org)
        return "runner-controlled-github-token"  # noqa: S105  # reaches only the bare remote

    async def create_repo(self, org: str, name: str, *, private: bool) -> SimpleNamespace:
        self._record("create_repo", org=org, name=name, private=private)
        _initialise(self.remote)
        return SimpleNamespace(id=1, full_name=f"{org}/{name}")

    async def get_repo(self, org: str, name: str) -> SimpleNamespace:
        self._record("get_repo", org=org, name=name)
        if not (self.remote / "HEAD").is_file():
            request = httpx.Request("GET", f"https://api.github.com/repos/{org}/{name}")
            raise httpx.HTTPStatusError(
                "not found", request=request, response=httpx.Response(404, request=request)
            )
        return SimpleNamespace(id=1, full_name=f"{org}/{name}", allow_auto_merge=True)

    async def set_repository_secrets(self, org: str, name: str, secrets, token=None) -> int:
        self._record("set_repository_secrets", org=org, name=name, count=len(secrets))
        return len(secrets)

    async def update_branch_protection(self, org: str, name: str, branch: str, **rules) -> None:
        self._record("update_branch_protection", org=org, name=name, branch=branch, **rules)

    async def enable_repo_auto_merge(self, org: str, name: str) -> None:
        self._record("enable_repo_auto_merge", org=org, name=name)


def _initialise(remote: Path) -> None:
    """A new GitHub repository with `auto_init`: one README commit on `main`."""
    git = ["git", "-c", "core.hooksPath=/dev/null"]
    subprocess.run([*git, "init", "-q", "--bare", "-b", "main", str(remote)], check=True)
    with tempfile.TemporaryDirectory(prefix="runner-auto-init-") as scratch:
        seed = Path(scratch)
        subprocess.run([*git, "init", "-q", "-b", "main", str(seed)], check=True)
        (seed / "README.md").write_text("# runner product\n")
        identity = ["-c", "user.name=GitHub", "-c", "user.email=noreply@github.com"]
        subprocess.run([*git, "-C", str(seed), "add", "README.md"], check=True)
        subprocess.run(
            [*git, *identity, "-C", str(seed), "commit", "-q", "-m", "Initial commit"], check=True
        )
        subprocess.run([*git, "-C", str(seed), "push", "-q", str(remote), "main"], check=True)


def insteadof_config(path: Path, remote: Path, git_url: str) -> Path:
    """The process-scoped git configuration that answers the owned GitHub URL locally."""
    path.write_text(f'[url "{remote}"]\n\tinsteadOf = {git_url}\n')
    return path


async def deliver_one(redis, consumer) -> dict:
    """The next scaffold-queue entry, read by the scaffolder's group and run by its entrypoint.

    As the scaffolder's worker loop does: the entry is validated against its queue contract,
    handed to `process_scaffold_job`, acknowledged, and a full or ensure scaffold releases the
    scheduler's in-flight marker.
    """
    from shared.contracts.queues.scaffold import ScaffoldMessage  # noqa: PLC0415
    from shared.queues import SCAFFOLD_GROUP, SCAFFOLD_QUEUE  # noqa: PLC0415

    stream = redis.consume(
        SCAFFOLD_QUEUE, SCAFFOLD_GROUP, f"{CONSUMER}-{os.getpid()}", block_ms=2000, auto_ack=False
    )
    try:
        for _ in range(30):
            message = await anext(stream)
            if message is None:
                continue
            delivered = dict(message.data)
            ScaffoldMessage.model_validate(delivered)
            result = await consumer.process_scaffold_job(message.data, redis)
            await redis.ack(SCAFFOLD_QUEUE, SCAFFOLD_GROUP, message.message_id)
            if delivered.get("mode") != "install":
                await redis.redis.delete(f"scaffold:inflight:{delivered['project_id']}")
            return {"entry_id": message.message_id, "message": delivered, "result": result}
    finally:
        await stream.aclose()
    raise RuntimeError("no scaffold-queue entry was delivered")


class RecordedAPI:
    """The scaffolder's API client, every install command also reaching a recorder."""

    def __init__(self, api, record) -> None:
        self._api = api
        self._record = record

    def __getattr__(self, name: str) -> Any:
        return getattr(self._api, name)

    async def catalog_install_command(self, task_id, body):
        answer = await self._api.catalog_install_command(task_id, body)
        await self._record(body)
        return answer
