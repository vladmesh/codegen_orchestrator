"""Git reference operations: create, read, move and delete refs."""

import re

import httpx

from shared.log_config import get_logger

logger = get_logger(__name__)

# Prefix that marks a tag as ours and temporary. A tag left behind by a crashed
# deploy is found by this prefix, so cleanup does not have to guess which tags in
# the user's repository are service tags.
DEPLOY_PIN_TAG_PREFIX = "codegen-deploy-pin-"

_COMMIT_SHA = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")


def deploy_pin_tag(head_sha: str) -> str:
    """Name of the temporary tag that pins one deploy to one commit.

    Derived from the SHA alone, so the same commit always yields the same tag and
    an abandoned tag can be matched back to its commit.
    """
    sha = head_sha.strip().lower()
    if not _COMMIT_SHA.fullmatch(sha):
        raise ValueError(f"deploy pin tag needs a full commit SHA, got {head_sha!r}")
    return f"{DEPLOY_PIN_TAG_PREFIX}{sha}"


class RefsMixin:
    """Git refs of a repository. ``ref`` is given without the leading ``refs/``."""

    async def _ref_headers(self, owner: str, repo: str) -> dict[str, str]:
        token = await self.get_token(owner, repo)
        return {
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github+json",
        }

    async def get_ref_sha(self, owner: str, repo: str, ref: str) -> str | None:
        """SHA the ref points at, or None when the ref does not exist."""
        headers = await self._ref_headers(owner, repo)
        try:
            resp = await self._make_request(
                "GET",
                f"https://api.github.com/repos/{owner}/{repo}/git/ref/{ref}",
                headers=headers,
            )
        except httpx.HTTPStatusError as e:
            if e.response.status_code == httpx.codes.NOT_FOUND:
                return None
            raise
        return resp.json()["object"]["sha"]

    async def create_ref(self, owner: str, repo: str, ref: str, sha: str) -> None:
        """Create a ref at a commit. 422 if it already exists."""
        headers = await self._ref_headers(owner, repo)
        await self._make_request(
            "POST",
            f"https://api.github.com/repos/{owner}/{repo}/git/refs",
            headers=headers,
            json={"ref": f"refs/{ref}", "sha": sha},
        )
        logger.info("git_ref_created", owner=owner, repo=repo, ref=ref, sha=sha)

    async def update_ref(
        self, owner: str, repo: str, ref: str, sha: str, force: bool = False
    ) -> None:
        """Move an existing ref to a commit."""
        headers = await self._ref_headers(owner, repo)
        await self._make_request(
            "PATCH",
            f"https://api.github.com/repos/{owner}/{repo}/git/refs/{ref}",
            headers=headers,
            json={"sha": sha, "force": force},
        )
        logger.info("git_ref_updated", owner=owner, repo=repo, ref=ref, sha=sha)

    async def delete_ref(self, owner: str, repo: str, ref: str) -> bool:
        """Delete a ref. False when it was already gone."""
        headers = await self._ref_headers(owner, repo)
        try:
            await self._make_request(
                "DELETE",
                f"https://api.github.com/repos/{owner}/{repo}/git/refs/{ref}",
                headers=headers,
            )
        except httpx.HTTPStatusError as e:
            if e.response.status_code == httpx.codes.NOT_FOUND:
                logger.info("git_ref_already_absent", owner=owner, repo=repo, ref=ref)
                return False
            raise
        logger.info("git_ref_deleted", owner=owner, repo=repo, ref=ref)
        return True

    async def create_or_reset_tag(self, owner: str, repo: str, tag: str, sha: str) -> None:
        """Point a lightweight tag at a commit, creating it or moving a leftover one.

        The pin tag name is deterministic, so a tag abandoned by an earlier crashed
        deploy of the same commit is reused instead of blocking the new one.
        """
        try:
            await self.create_ref(owner, repo, f"tags/{tag}", sha)
            return
        except httpx.HTTPStatusError as e:
            if e.response.status_code != httpx.codes.UNPROCESSABLE_ENTITY:
                raise
        await self.update_ref(owner, repo, f"tags/{tag}", sha, force=True)
