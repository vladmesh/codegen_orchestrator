"""The GitHub Actions repository secrets a deploy needs, and the receipt that skips unchanged ones.

The deployer is the only writer of six secrets (`DOTENV`, `DEPLOY_HOST`,
`DEPLOY_USER`, `DEPLOY_SSH_KEY`, `DEPLOY_PORT`, `PROJECT_NAME`). Redis key
`deploy:<project_id>:github-secrets` holds the SHA-256 of the last complete
successful write of all six, bound to the GitHub repository id, so a redeploy
with the same values to the same repository writes only the registry secrets.
The receipt is read, deleted and set only inside a Lua script that first
compares the project deploy lock with this deploy's token.
"""

from collections.abc import Iterable
from functools import partial
import hashlib
import json

import structlog

from shared.clients.github import (
    GitHubAppClient,
    RegistrySecretsNotRefreshedError,
    registry_repository_secrets,
)
from shared.diagnostics import redact_diagnostic

from ...deploy_fence import DeployFence, DeployFenceLost, DeployWrite

logger = structlog.get_logger()

_NOT_HELD = -1

# A hit leaves the receipt; anything else is deleted before the write starts, so
# an interrupted write can never leave a receipt for values it replaced.
_CLAIM_RECEIPT = """
if redis.call('GET', KEYS[1]) ~= ARGV[1] then
    return -1
end
if redis.call('GET', KEYS[2]) == ARGV[2] then
    return 1
end
redis.call('DEL', KEYS[2])
return 0
"""

_PUBLISH_RECEIPT = """
if redis.call('GET', KEYS[1]) ~= ARGV[1] then
    return -1
end
redis.call('SET', KEYS[2], ARGV[2])
return 1
"""


def receipt_key(project_id: str) -> str:
    return f"deploy:{project_id}:github-secrets"


def payload_digest(repository_id: int, secrets: dict[str, str]) -> str:
    """SHA-256 of the repository id and every resolved secret name and value, order-free."""
    canonical = json.dumps(
        {"repository_id": repository_id, "secrets": secrets},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


async def _fenced_receipt(fence: DeployFence, script: str, digest: str) -> int:
    result = await fence.redis.eval(
        script, 2, fence.lock_key, receipt_key(fence.project_id), fence.token, digest
    )
    if result == _NOT_HELD:
        raise fence.lost(DeployWrite.GITHUB_SECRETS)
    return result


async def write_deploy_secrets(
    github: GitHubAppClient,
    owner: str,
    repo: str,
    deployer_secrets: dict[str, str],
    *,
    deploy_fence: DeployFence,
    diagnostic_secrets: Iterable[str] = (),
) -> bool:
    """Make the deployer's six secrets and the three registry secrets current in `owner/repo`.

    True only when all nine are known set: written now, or the six certified by a
    matching receipt. A lost deploy lock raises `DeployFenceLost`; every other
    failure, Redis and the repository read included, is False and publishes nothing.
    """
    # The scaffolder and the PR poller write the registry secrets too, so no
    # deploy-only receipt can vouch for them; rewriting them on every deploy keeps
    # an administrative or capability redeploy's repository current as well.
    try:
        registry_secrets = registry_repository_secrets()
    except RegistrySecretsNotRefreshedError as error:
        logger.error("registry_env_missing", reason=error.reason.value, detail=error.detail)
        return False

    guard = partial(deploy_fence.ensure_held, DeployWrite.GITHUB_SECRETS)
    try:
        # A repository deleted and recreated under the same name gets a new id.
        repository = await github.get_repo(owner, repo)
        await guard()
        digest = payload_digest(repository.id, deployer_secrets)
        cached = await _fenced_receipt(deploy_fence, _CLAIM_RECEIPT, digest) == 1
        pending = registry_secrets if cached else {**deployer_secrets, **registry_secrets}
        written = await github.set_repository_secrets(owner, repo, pending, guard=guard)
        logger.info(
            "deploy_secrets_configured",
            owner=owner,
            repo=repo,
            secrets_count=written,
            cached_count=len(deployer_secrets) if cached else 0,
            total=len(deployer_secrets) + len(registry_secrets),
        )
        if written != len(pending):
            return False
        if not cached:
            await _fenced_receipt(deploy_fence, _PUBLISH_RECEIPT, digest)
        return True
    except DeployFenceLost:
        raise
    except Exception as e:
        logger.error(
            "deploy_secrets_setup_failed",
            owner=owner,
            repo=repo,
            error=redact_diagnostic(e, secrets=diagnostic_secrets),
            error_type=type(e).__name__,
        )
        return False
