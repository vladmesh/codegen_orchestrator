"""Native publication in a server-owned checkout. No executor or container start."""

import asyncio
import base64
import os
from pathlib import Path
import secrets
from urllib.parse import urlsplit

from fastapi import APIRouter, Header, HTTPException, Request

from shared.commit_publication import publish_commit
from shared.contracts.dto.commit_publication import (
    CommitPublication,
    CommitRecoveryRead,
    PublicationFailure,
)
from shared.contracts.dto.repository import RepositoryDTO
from shared.contracts.dto.run import RunDTO

router = APIRouter(prefix="/api/commit-recoveries", tags=["commit recovery"])
_REPOSITORY_PARTS = 2


@router.post("/{attempt_id}/publish", response_model=CommitPublication)
async def publish_preserved_commit(
    attempt_id: str, req: Request, x_internal_key: str | None = Header(default=None)
):
    if not x_internal_key or not secrets.compare_digest(
        x_internal_key, os.environ["INTERNAL_API_KEY"]
    ):
        raise HTTPException(403, "internal authentication required")
    state = req.app.state
    response = await state.credential_api.request("GET", f"commit-recoveries/{attempt_id}")
    claim = CommitRecoveryRead.model_validate(response.json())
    identity = claim.identity.model_dump(mode="json")
    sha = claim.commit_sha

    def refuse(failure):
        return CommitPublication(failure=failure, attempt_id=attempt_id)

    run_response = await state.credential_api.request("GET", f"runs/{attempt_id}")
    run = RunDTO.model_validate(run_response.json())
    repository_response = await state.credential_api.request(
        "GET", f"repositories/{identity['repository_id']}"
    )
    repository = RepositoryDTO.model_validate(repository_response.json())
    if (
        run.id != identity["attempt_id"]
        or run.story_id != identity["story_id"]
        or str(run.project_id) != identity["project_id"]
        or run.run_metadata["worker_id"] != identity["worker_id"]
        or repository.id != identity["repository_id"]
        or str(repository.project_id) != identity["project_id"]
        or repository.git_url != identity["repository_url"]
    ):
        return refuse(PublicationFailure.OWNERSHIP_MISSING)
    if run.type.value != "engineering" or run.status.value != "failed":
        return refuse(PublicationFailure.STALE_ATTEMPT)
    root = Path(state.scaffolded_workspace_path).resolve()
    workspace = (root / repository.id).resolve()
    if workspace.parent != root or not workspace.is_dir():
        return refuse(PublicationFailure.OBJECT_MISSING)
    # Never race an owned worker or reuse/prepare/reset its checkout.
    if await state.redis.get(f"workspace:lock:{identity['project_id']}"):
        return refuse(PublicationFailure.STALE_ATTEMPT)
    url = urlsplit(repository.git_url)
    if url.scheme != "https" or url.netloc != "github.com" or url.query or url.fragment:
        return refuse(PublicationFailure.WRONG_REPOSITORY)
    full_name = url.path.strip("/").removesuffix(".git")
    parts = full_name.split("/")
    if len(parts) != _REPOSITORY_PARTS:
        return refuse(PublicationFailure.WRONG_REPOSITORY)
    try:
        token = await state.github.get_repo_scoped_token(*parts)
    except Exception:  # noqa: BLE001 - never expose minting exception values
        return refuse(PublicationFailure.CREDENTIAL_UNAVAILABLE)
    encoded = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(Path.home()),
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_COUNT": "4",
        "GIT_CONFIG_KEY_0": f"http.{repository.git_url}.extraheader",
        "GIT_CONFIG_VALUE_0": "",
        "GIT_CONFIG_KEY_1": f"http.{repository.git_url}.extraheader",
        "GIT_CONFIG_VALUE_1": f"Authorization: Basic {encoded}",
        "GIT_CONFIG_KEY_2": "credential.helper",
        "GIT_CONFIG_VALUE_2": "",
        "GIT_CONFIG_KEY_3": "safe.directory",
        "GIT_CONFIG_VALUE_3": str(workspace),
    }
    receipt = await asyncio.to_thread(
        publish_commit,
        workspace,
        identity["branch"],
        sha,
        baseline=identity["baseline"],
        repository_url=repository.git_url,
        env=env,
        secrets=(token, encoded),
    )
    return receipt.model_copy(update={"attempt_id": attempt_id, "worker_id": identity["worker_id"]})
