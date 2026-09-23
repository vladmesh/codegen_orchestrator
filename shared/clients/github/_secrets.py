import base64
from collections.abc import Mapping
from enum import StrEnum
import os

from shared.diagnostics import redact_diagnostic
from shared.log_config import get_logger

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


class SecretsMixin:
    """GitHub Actions repository secrets management."""

    async def set_repository_secret(
        self,
        owner: str,
        repo: str,
        secret_name: str,
        secret_value: str,
        token: str | None = None,
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
    ) -> int:
        """Set multiple repository secrets at once.

        Args:
            owner: Repository owner
            repo: Repository name
            secrets: Dictionary of secret_name -> secret_value
            token: Optional pre-obtained token (e.g. org-level). Falls back to per-repo lookup.

        Returns:
            Number of secrets successfully set
        """
        count = 0
        for name, value in secrets.items():
            try:
                await self.set_repository_secret(owner, repo, name, value, token=token)
                count += 1
            except Exception as e:
                logger.error(
                    "github_secret_set_failed",
                    owner=owner,
                    repo=repo,
                    secret_name=name,
                    error=redact_diagnostic(e, secrets=secrets.values()),
                )
        return count

    async def refresh_registry_secrets(
        self,
        owner: str,
        repo: str,
        token: str | None = None,
    ) -> None:
        """Write the orchestrator's current registry secrets into one product repository.

        Idempotent and cheap, so it runs before every merge that starts the
        product's push-main CI: those builds read ``REGISTRY_URL`` and the
        credentials when they start, and a repository created elsewhere, or
        created before a hostname or credential change, would otherwise build
        against stale values. Raises ``RegistrySecretsNotRefreshedError`` unless
        every secret was written.
        """
        secrets = registry_repository_secrets()
        written = await self.set_repository_secrets(owner, repo, secrets, token=token)
        if written != len(secrets):
            raise RegistrySecretsNotRefreshedError(
                RegistrySecretsRefusal.WRITE_INCOMPLETE,
                f"wrote {written} of {len(secrets)} registry secrets to {owner}/{repo}",
            )
        logger.info("registry_secrets_refreshed", owner=owner, repo=repo, count=written)
