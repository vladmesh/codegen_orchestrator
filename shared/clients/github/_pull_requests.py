import httpx

from shared.log_config import get_logger

logger = get_logger(__name__)


def _validation_detail(response: httpx.Response) -> str:
    """Join GitHub's validation error messages from a 422 body."""
    try:
        body = response.json()
    except ValueError:
        return ""
    messages = [
        str(err.get("message", "")) for err in body.get("errors", []) if isinstance(err, dict)
    ]
    return "; ".join(m for m in messages if m) or str(body.get("message", ""))


class PullRequestsMixin:
    """Pull request operations."""

    async def create_pull_request(
        self,
        owner: str,
        repo: str,
        head: str,
        base: str,
        title: str,
        body: str = "",
    ) -> dict:
        """Create a pull request.

        If a PR already exists for the same head->base, returns the existing PR.

        Returns:
            PR dict with number, html_url, node_id, etc.
        """
        token = await self.get_token(owner, repo)
        headers = {
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github+json",
        }

        try:
            resp = await self._make_request(
                "POST",
                f"https://api.github.com/repos/{owner}/{repo}/pulls",
                headers=headers,
                json={"head": head, "base": base, "title": title, "body": body},
            )
        except httpx.HTTPStatusError as e:
            if e.response.status_code != httpx.codes.UNPROCESSABLE_ENTITY:
                raise
            # 422 covers several distinct rejections. "No commits between" means the
            # branch carries nothing to merge — usually an agent whose push was
            # rejected — and no amount of PR lookup will find a PR that never existed.
            detail = _validation_detail(e.response)
            if "no commits between" in detail.lower():
                logger.error(
                    "pr_branch_has_no_commits",
                    owner=owner,
                    repo=repo,
                    head=head,
                    base=base,
                    detail=detail,
                )
                raise RuntimeError(
                    f"Cannot open PR {head}->{base}: {detail}. The branch has no commits "
                    f"of its own, so nothing was pushed to it."
                ) from e
            # PR already exists — find and return it (check open first, then closed/merged)
            logger.info("pr_already_exists", owner=owner, repo=repo, head=head, detail=detail)
            for state in ("open", "closed"):
                list_resp = await self._make_request(
                    "GET",
                    f"https://api.github.com/repos/{owner}/{repo}/pulls",
                    headers=headers,
                    params={"head": f"{owner}:{head}", "base": base, "state": state},
                )
                prs = list_resp.json()
                if prs:
                    return prs[0]
            raise RuntimeError(
                f"PR creation returned 422 but no existing PR found for {head}->{base}"
            ) from e

        pr = resp.json()
        logger.info(
            "pr_created",
            owner=owner,
            repo=repo,
            pr_number=pr["number"],
            head=head,
            base=base,
        )
        return pr

    async def get_pull_request(self, owner: str, repo: str, pr_number: int) -> dict:
        """Fetch a single pull request by number."""
        token = await self.get_token(owner, repo)
        headers = {
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github+json",
        }
        resp = await self._make_request(
            "GET",
            f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}",
            headers=headers,
        )
        return resp.json()

    async def enable_auto_merge(
        self,
        owner: str,
        repo: str,
        pr_node_id: str,
        merge_method: str = "MERGE",
    ) -> bool:
        """Enable auto-merge on a pull request via GraphQL.

        Args:
            owner: Repository owner (for token resolution)
            repo: Repository name (for token resolution)
            pr_node_id: The GraphQL node_id of the pull request
            merge_method: MERGE, SQUASH, or REBASE

        Returns:
            True if auto-merge was enabled, False if not allowed.
        """
        token = await self.get_token(owner, repo)
        headers = {
            "Authorization": f"bearer {token}",
            "Accept": "application/vnd.github+json",
        }

        query = """
        mutation EnableAutoMerge($pullRequestId: ID!, $mergeMethod: PullRequestMergeMethod!) {
            enablePullRequestAutoMerge(input: {
                pullRequestId: $pullRequestId,
                mergeMethod: $mergeMethod
            }) {
                pullRequest {
                    number
                    autoMergeRequest { mergeMethod }
                }
            }
        }
        """

        resp = await self._make_request(
            "POST",
            "https://api.github.com/graphql",
            headers=headers,
            json={
                "query": query,
                "variables": {"pullRequestId": pr_node_id, "mergeMethod": merge_method},
            },
        )

        data = resp.json()
        if "errors" in data:
            logger.warning(
                "auto_merge_failed",
                owner=owner,
                repo=repo,
                errors=data["errors"],
            )
            return False

        logger.info("auto_merge_enabled", owner=owner, repo=repo, pr_node_id=pr_node_id)
        return True

    async def merge_pull_request(
        self,
        owner: str,
        repo: str,
        pr_number: int,
        merge_method: str = "merge",
    ) -> dict:
        """Merge a pull request.

        Args:
            merge_method: "merge", "squash", or "rebase"

        Returns:
            Merge result dict with sha, merged fields.
        """
        token = await self.get_token(owner, repo)
        headers = {
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github+json",
        }

        resp = await self._make_request(
            "PUT",
            f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}/merge",
            headers=headers,
            json={"merge_method": merge_method},
        )
        resp.raise_for_status()

        result = resp.json()
        logger.info(
            "pr_merged",
            owner=owner,
            repo=repo,
            pr_number=pr_number,
            sha=result.get("sha"),
        )
        return result

    async def list_pull_requests(
        self,
        owner: str,
        repo: str,
        head: str | None = None,
        base: str | None = None,
        state: str = "closed",
    ) -> list[dict]:
        """List pull requests with optional filters.

        Args:
            head: Filter by head branch (format: "owner:branch" or just "branch").
            base: Filter by base branch (e.g. "main").
            state: PR state: open, closed, all.
        """
        token = await self.get_token(owner, repo)
        headers = {
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github+json",
        }
        params: dict[str, str] = {"state": state}
        if head:
            # GitHub API expects "owner:branch" format
            params["head"] = head if ":" in head else f"{owner}:{head}"
        if base:
            params["base"] = base

        resp = await self._make_request(
            "GET",
            f"https://api.github.com/repos/{owner}/{repo}/pulls",
            headers=headers,
            params=params,
        )
        return resp.json()

    async def close_pull_request(self, owner: str, repo: str, pr_number: int) -> dict:
        """Close a pull request without merging."""
        token = await self.get_token(owner, repo)
        headers = {
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github+json",
        }

        resp = await self._make_request(
            "PATCH",
            f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}",
            headers=headers,
            json={"state": "closed"},
        )
        resp.raise_for_status()

        result = resp.json()
        logger.info("pr_closed", owner=owner, repo=repo, pr_number=pr_number)
        return result
