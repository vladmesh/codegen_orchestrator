import base64

from shared.log_config import get_logger

logger = get_logger(__name__)


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
                    error=str(e),
                )
        return count
