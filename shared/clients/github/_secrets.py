import base64
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
import hashlib
import hmac
import json
import os
from typing import TYPE_CHECKING
from uuid import uuid4

from cryptography.fernet import InvalidToken

from shared.config import registry_secrets_lease_seconds
from shared.crypto import SecretsCipher
from shared.diagnostics import redact_diagnostic
from shared.log_config import get_logger
from shared.redis import decode_redis_value

from ._base import RequestGuard, RequestGuardRefusedError

if TYPE_CHECKING:
    from redis.asyncio import Redis

logger = get_logger(__name__)

#: The repository secrets a generated product's push-main CI logs in to the
#: orchestrator's registry with, each paired with the orchestrator environment
#: variable that holds its current value.
REGISTRY_SECRET_ENV: tuple[tuple[str, str], ...] = (
    ("REGISTRY_URL", "ORCHESTRATOR_HOSTNAME"),
    ("REGISTRY_USER", "REGISTRY_USER"),
    ("REGISTRY_PASSWORD", "REGISTRY_PASSWORD"),
)


class RegistrySecretsRefusal(StrEnum):
    """Why a product repository's registry secrets could not be made current."""

    ENV_MISSING = "registry_secrets_env_missing"
    WRITE_INCOMPLETE = "registry_secrets_write_incomplete"
    RECEIPT_KEY_INVALID = "registry_secrets_receipt_key_invalid"
    LEASE_BUSY = "registry_secrets_lease_busy"
    LEASE_LOST = "registry_secrets_lease_lost"
    CALLER_LOCK_LOST = "registry_secrets_caller_lock_lost"


class RegistrySecretsNotRefreshedError(RuntimeError):
    """The registry secrets are not known to be current, so nothing may start CI.

    The message names variables and counts only, never a value.
    """

    def __init__(self, reason: RegistrySecretsRefusal, detail: str) -> None:
        super().__init__(detail)
        self.reason = reason
        self.detail = detail


def registry_repository_secrets(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """The registry secrets a product repository needs, read from this process's env.

    Raises ``RegistrySecretsNotRefreshedError`` naming every missing variable.
    """
    env = os.environ if environ is None else environ
    missing = [variable for _, variable in REGISTRY_SECRET_ENV if not env.get(variable)]
    if missing:
        raise RegistrySecretsNotRefreshedError(
            RegistrySecretsRefusal.ENV_MISSING, f"{', '.join(missing)} not set"
        )
    return {secret: env[variable] for secret, variable in REGISTRY_SECRET_ENV}


#: Part of the digest input, so a registry receipt never matches a digest made for another use.
_RECEIPT_CONTEXT = "github-registry-secrets-receipt/v1"

# Each script first checks both owners in one call: the caller's lock (KEYS[3], when the
# caller has one) against ARGV[2], then the lease against this holder's token in ARGV[1],
# answering -1, 0 or 1 for a lost lock, a lost lease or both held. A holder that lost
# either can neither read a hit, invalidate, publish nor send. A receipt that is not a
# Fernet token reads as absent, so corrupt bytes are a miss whether or not the caller's
# connection decodes responses.
_CALLER_LOST, _HELD = -1, 1

_OWNERS_HELD = """
local function owners_held()
    if #KEYS == 3 and redis.call('GET', KEYS[3]) ~= ARGV[2] then
        return -1
    end
    if redis.call('GET', KEYS[1]) ~= ARGV[1] then
        return 0
    end
    return 1
end
local held = owners_held()
"""

_READ_RECEIPT = (
    _OWNERS_HELD
    + """
if held ~= 1 then
    return {held}
end
local receipt = redis.call('GET', KEYS[2])
if not receipt or not string.match(receipt, '^[%w_=-]+$') then
    return {1}
end
return {1, receipt}
"""
)

_HOLDS_LEASE = _OWNERS_HELD + "return held\n"

_INVALIDATE_RECEIPT = (
    _OWNERS_HELD
    + """
if held == 1 then
    redis.call('DEL', KEYS[2])
end
return held
"""
)

_PUBLISH_RECEIPT = (
    _OWNERS_HELD
    + """
if held == 1 then
    redis.call('SET', KEYS[2], ARGV[3])
end
return held
"""
)

_RELEASE_LEASE = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('DEL', KEYS[1])
end
return 0
"""


def registry_receipt_key(repository_id: int) -> str:
    """The Redis key of the one registry-secrets receipt of a GitHub repository id."""
    return f"github:repository:{repository_id}:registry-secrets"


def registry_lease_key(repository_id: int) -> str:
    """The Redis key of the one registry-secrets lease of a GitHub repository id."""
    return f"{registry_receipt_key(repository_id)}:lease"


def _registry_digest(repository_id: int, secrets: Mapping[str, str]) -> str:
    canonical = json.dumps(
        {"context": _RECEIPT_CONTEXT, "repository_id": repository_id, "secrets": dict(secrets)},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _receipt_cipher() -> SecretsCipher:
    try:
        return SecretsCipher()
    except (RuntimeError, ValueError):
        raise RegistrySecretsNotRefreshedError(
            RegistrySecretsRefusal.RECEIPT_KEY_INVALID,
            "SECRETS_ENCRYPTION_KEY is not a usable Fernet key",
        ) from None


def registry_writer_lease_seconds() -> int:
    """This writer process's registry lease policy, refused unless the receipt key is usable too.

    A writer calls it at startup, so a missing policy or key stops the process before any write.
    """
    _receipt_cipher()
    return registry_secrets_lease_seconds()


def _vouches(cipher: SecretsCipher, receipt: str, digest: str) -> bool:
    try:
        held = cipher.decrypt(receipt)
    except (InvalidToken, UnicodeError):
        return False
    return hmac.compare_digest(held.encode(), digest.encode())


@dataclass(frozen=True)
class RegistrySecretsResult:
    """Registry secrets made current: `written` is 0 on a receipt hit, else all of them."""

    written: int


@dataclass(frozen=True)
class _RegistryLease:
    """One holder's claim on a repository's registry lease, checked by every receipt script.

    `caller_lock` is the caller's own `(key, token)` lock, checked in the same script.
    """

    redis: "Redis"
    repository_id: int
    owner: str
    repo: str
    token: str
    caller_lock: tuple[str, str] | None

    async def _eval(self, script: str, *args: str) -> object:
        keys = [registry_lease_key(self.repository_id), registry_receipt_key(self.repository_id)]
        caller_token = ""
        if self.caller_lock is not None:
            caller_key, caller_token = self.caller_lock
            keys.append(caller_key)
        return await self.redis.eval(script, len(keys), *keys, self.token, caller_token, *args)

    def _refusal(self, held: object) -> RegistrySecretsNotRefreshedError:
        repository = f"{self.owner}/{self.repo}"
        if held == _CALLER_LOST:
            reason, detail = (
                RegistrySecretsRefusal.CALLER_LOCK_LOST,
                f"the caller lost its lock while making {repository} current",
            )
        else:
            reason, detail = (
                RegistrySecretsRefusal.LEASE_LOST,
                f"lost the registry secrets lease of {repository}",
            )
        logger.warning(
            reason.value, owner=self.owner, repo=self.repo, repository_id=self.repository_id
        )
        return RegistrySecretsNotRefreshedError(reason, detail)

    async def _held(self, script: str, *args: str) -> None:
        held = await self._eval(script, *args)
        if held != _HELD:
            raise self._refusal(held)

    async def acquire(self, lease_seconds: int) -> None:
        key = registry_lease_key(self.repository_id)
        if not await self.redis.set(key, self.token, nx=True, ex=lease_seconds):
            logger.warning(
                "registry_secrets_lease_busy",
                owner=self.owner,
                repo=self.repo,
                repository_id=self.repository_id,
            )
            raise RegistrySecretsNotRefreshedError(
                RegistrySecretsRefusal.LEASE_BUSY,
                f"another holder holds the registry secrets lease of {self.owner}/{self.repo}",
            )

    async def read_receipt(self) -> str | None:
        held, *receipt = await self._eval(_READ_RECEIPT)
        if held != _HELD:
            raise self._refusal(held)
        return decode_redis_value(receipt[0]) if receipt else None

    async def ensure_held(self) -> None:
        await self._held(_HOLDS_LEASE)

    async def invalidate_receipt(self) -> None:
        await self._held(_INVALIDATE_RECEIPT)

    async def publish_receipt(self, receipt: str) -> None:
        await self._held(_PUBLISH_RECEIPT, receipt)

    async def release(self) -> None:
        await self.redis.eval(_RELEASE_LEASE, 1, registry_lease_key(self.repository_id), self.token)


class SecretsMixin:
    """GitHub Actions repository secrets management."""

    async def set_repository_secret(
        self,
        owner: str,
        repo: str,
        secret_name: str,
        secret_value: str,
        token: str | None = None,
        *,
        guard: RequestGuard | None = None,
    ) -> None:
        """Set an encrypted repository secret for GitHub Actions.

        Uses libsodium (via pynacl) to encrypt the secret value before
        sending it to GitHub API.

        Args:
            owner: Repository owner
            repo: Repository name
            secret_name: Name of the secret (e.g., DEPLOY_HOST)
            secret_value: Plain text value to encrypt and store
            token: Optional pre-obtained token (e.g. org-level). Falls back to per-repo lookup.
            guard: Awaited immediately before each attempt of the PUT; see `_make_request`.
        """
        # Lazy import: pynacl only needed when this method is called
        from nacl import public

        if not token:
            token = await self.get_token(owner, repo)
        headers = {
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github+json",
        }

        # 1. Get repository public key for encryption
        resp = await self._make_request(
            "GET",
            f"https://api.github.com/repos/{owner}/{repo}/actions/secrets/public-key",
            headers=headers,
        )
        key_data = resp.json()
        public_key_b64 = key_data["key"]
        key_id = key_data["key_id"]

        # 2. Encrypt the secret using libsodium sealed box
        public_key_bytes = base64.b64decode(public_key_b64)
        pub_key = public.PublicKey(public_key_bytes)
        sealed_box = public.SealedBox(pub_key)
        encrypted = sealed_box.encrypt(secret_value.encode("utf-8"))
        encrypted_b64 = base64.b64encode(encrypted).decode("utf-8")

        # 3. Create or update the secret
        await self._make_request(
            "PUT",
            f"https://api.github.com/repos/{owner}/{repo}/actions/secrets/{secret_name}",
            headers=headers,
            guard=guard,
            json={
                "encrypted_value": encrypted_b64,
                "key_id": key_id,
            },
        )

        logger.info(
            "github_secret_set",
            owner=owner,
            repo=repo,
            secret_name=secret_name,
        )

    async def set_repository_secrets(
        self,
        owner: str,
        repo: str,
        secrets: dict[str, str],
        token: str | None = None,
        *,
        guard: RequestGuard | None = None,
    ) -> int:
        """Set multiple repository secrets at once.

        Args:
            owner: Repository owner
            repo: Repository name
            secrets: Dictionary of secret_name -> secret_value
            token: Optional pre-obtained token (e.g. org-level). Falls back to per-repo lookup.
            guard: Awaited immediately before each PUT attempt. Its own refusal is raised
                unchanged and stops the batch, so no later secret is written.

        Returns:
            Number of secrets successfully set
        """
        count = 0
        for name, value in secrets.items():
            try:
                await self.set_repository_secret(owner, repo, name, value, token=token, guard=guard)
                count += 1
            except RequestGuardRefusedError as refused:
                raise refused.error from None
            except Exception as e:
                logger.error(
                    "github_secret_set_failed",
                    owner=owner,
                    repo=repo,
                    secret_name=name,
                    error=redact_diagnostic(e, secrets=secrets.values()),
                )
        return count

    async def ensure_registry_secrets(
        self,
        owner: str,
        repo: str,
        *,
        redis: "Redis",
        lease_seconds: int,
        guard: RequestGuard | None = None,
        caller_lock: tuple[str, str] | None = None,
    ) -> RegistrySecretsResult:
        """Make the registry secrets current in `owner/repo`, skipped while a receipt vouches.

        Runs under the repository's registry lease and raises
        ``RegistrySecretsNotRefreshedError`` unless it proved a receipt hit or wrote every secret.
        A `caller_lock` `(key, token)` must hold with the lease at each hit, PUT and receipt change.
        """
        if lease_seconds <= 0:
            raise ValueError(f"lease_seconds must be positive, got {lease_seconds}")
        secrets = registry_repository_secrets()
        cipher = _receipt_cipher()
        # A repository deleted and recreated under the same name gets a new id.
        repository_id = (await self.get_repo(owner, repo)).id
        lease = _RegistryLease(
            redis, repository_id, owner, repo, token=uuid4().hex, caller_lock=caller_lock
        )
        await lease.acquire(lease_seconds)
        try:
            digest = _registry_digest(repository_id, secrets)
            receipt = await lease.read_receipt()
            if receipt is not None and _vouches(cipher, receipt, digest):
                await lease.ensure_held()
                written = 0
            else:
                await lease.invalidate_receipt()

                # The joint owner check is the last await before each PUT attempt, so a
                # lock or lease lost while the caller's guard was suspended sends nothing.
                async def guarded() -> None:
                    if guard is not None:
                        await guard()
                    await lease.ensure_held()

                written = await self.set_repository_secrets(owner, repo, secrets, guard=guarded)
                if written != len(secrets):
                    raise RegistrySecretsNotRefreshedError(
                        RegistrySecretsRefusal.WRITE_INCOMPLETE,
                        f"wrote {written} of {len(secrets)} registry secrets to {owner}/{repo}",
                    )
                await lease.publish_receipt(cipher.encrypt(digest))
        except BaseException:
            # The active failure, a caller refusal or a cancellation, is what the caller
            # needs; a cleanup failure is only logged, and the lease expires on its own.
            try:
                await lease.release()
            except Exception as error:
                logger.error(
                    "registry_secrets_lease_release_failed",
                    owner=owner,
                    repo=repo,
                    repository_id=repository_id,
                    error=redact_diagnostic(error, secrets=secrets.values()),
                    error_type=type(error).__name__,
                )
            raise
        await lease.release()
        logger.info(
            "registry_secrets_current",
            owner=owner,
            repo=repo,
            repository_id=repository_id,
            written=written,
        )
        return RegistrySecretsResult(written=written)
