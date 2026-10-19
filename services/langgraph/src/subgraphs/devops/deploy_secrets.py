"""The GitHub Actions repository secrets a deploy needs, and the receipts that skip unchanged ones.

The deployer is the only writer of six secrets (`DOTENV`, `DEPLOY_HOST`,
`DEPLOY_USER`, `DEPLOY_SSH_KEY`, `DEPLOY_PORT`, `PROJECT_NAME`). Redis key
`deploy:<project_id>:github-secrets` holds the SHA-256 of the six values a deploy
last set together with the registry secrets, bound to the GitHub repository id,
so a redeploy with the same values to the same repository writes none of the six.
The receipt is read, deleted and set only inside a Lua script that first
compares the project deploy lock with this deploy's token. The three registry
secrets go through the shared registry receipt, checked jointly with the deploy lock.
"""

from collections.abc import Iterable
from functools import partial
import hashlib
import json

import structlog

from shared.clients.github import (
    REGISTRY_SECRET_ENV,
    GitHubAppClient,
    RegistrySecretsNotRefreshedError,
    RegistrySecretsRefusal,
    registry_writer_lease_seconds,
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

    True only when all nine are known set: written now, or certified by a matching
    receipt. A lost deploy lock raises `DeployFenceLost`; every other failure,
    Redis and the repository read included, is False and publishes no six-value receipt.
    """
    guard = partial(deploy_fence.ensure_held, DeployWrite.GITHUB_SECRETS)
    try:
        registry = await github.ensure_registry_secrets(
            owner,
            repo,
            redis=deploy_fence.redis,
            lease_seconds=registry_writer_lease_seconds(),
            caller_lock=(deploy_fence.lock_key, deploy_fence.token),
        )
        # A repository deleted and recreated under the same name gets a new id.
        repository = await github.get_repo(owner, repo)
        await guard()
        digest = payload_digest(repository.id, deployer_secrets)
        cached = await _fenced_receipt(deploy_fence, _CLAIM_RECEIPT, digest) == 1
        written = 0
        if not cached:
            written = await github.set_repository_secrets(
                owner, repo, deployer_secrets, guard=guard
            )
        logger.info(
            "deploy_secrets_configured",
            owner=owner,
            repo=repo,
            secrets_count=written + registry.written,
            cached_count=(len(deployer_secrets) if cached else 0)
            + (len(REGISTRY_SECRET_ENV) - registry.written),
            total=len(deployer_secrets) + len(REGISTRY_SECRET_ENV),
        )
        if not cached:
            if written != len(deployer_secrets):
                return False
            await _fenced_receipt(deploy_fence, _PUBLISH_RECEIPT, digest)
        return True
    except DeployFenceLost:
        raise
    except RegistrySecretsNotRefreshedError as error:
        if error.reason is RegistrySecretsRefusal.CALLER_LOCK_LOST:
            raise deploy_fence.lost(DeployWrite.GITHUB_SECRETS) from None
        logger.error(
            "deploy_registry_secrets_not_current",
            owner=owner,
            repo=repo,
            reason=error.reason.value,
            detail=error.detail,
        )
        return False
    except Exception as e:
        logger.error(
            "deploy_secrets_setup_failed",
            owner=owner,
            repo=repo,
            error=redact_diagnostic(e, secrets=diagnostic_secrets),
            error_type=type(e).__name__,
        )
        return False
