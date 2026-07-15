import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

import httpx

from shared.log_config import get_logger

from ._base import (
    WorkflowCancellationUnprovenError,
    WorkflowCancelledError,
    WorkflowNotFoundError,
)

logger = get_logger(__name__)


class ActionsMixin:
    """GitHub Actions workflows, branch protection, and CI operations."""

    async def update_branch_protection(
        self,
        owner: str,
        repo: str,
        branch: str,
        required_checks: list[str],
        require_pr: bool = True,
        enforce_admins: bool = False,
    ) -> None:
        """Set branch protection rules via GitHub API.

        Args:
            owner: Repository owner (org)
            repo: Repository name
            branch: Branch to protect
            required_checks: Status check contexts to require (e.g. ["lint-and-test"])
            require_pr: Require PR for merges
            enforce_admins: Apply rules to admins too

        Raises:
            httpx.HTTPStatusError: On API errors (404 if branch doesn't exist)
        """
        token = await self.get_org_token(owner)
        headers = {
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github+json",
        }

        payload: dict = {
            "required_status_checks": {
                "strict": True,
                "contexts": required_checks,
            },
            "enforce_admins": enforce_admins,
            "restrictions": None,
        }

        if require_pr:
            payload["required_pull_request_reviews"] = {
                "required_approving_review_count": 0,
            }
        else:
            payload["required_pull_request_reviews"] = None

        resp = await self._make_request(
            "PUT",
            f"https://api.github.com/repos/{owner}/{repo}/branches/{branch}/protection",
            headers=headers,
            json=payload,
        )
        resp.raise_for_status()

        logger.info(
            "branch_protection_updated",
            owner=owner,
            repo=repo,
            branch=branch,
            checks=required_checks,
            require_pr=require_pr,
        )

    async def enable_repo_auto_merge(self, owner: str, repo: str) -> None:
        """Enable allow_auto_merge repo setting so PRs can use auto-merge."""
        token = await self.get_org_token(owner)
        headers = {
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github+json",
        }
        resp = await self._make_request(
            "PATCH",
            f"https://api.github.com/repos/{owner}/{repo}",
            headers=headers,
            json={"allow_auto_merge": True},
        )
        resp.raise_for_status()
        logger.info("repo_auto_merge_enabled", owner=owner, repo=repo)

    async def trigger_workflow_dispatch(
        self,
        owner: str,
        repo: str,
        workflow_file: str,
        ref: str = "main",
        inputs: dict | None = None,
    ) -> bool:
        """Trigger a workflow_dispatch event.

        Args:
            owner: Repository owner
            repo: Repository name
            workflow_file: Workflow filename (e.g. "deploy.yml")
            ref: Git ref to run the workflow on
            inputs: Optional workflow inputs

        Returns:
            True if dispatch was accepted (204)

        Raises:
            httpx.HTTPStatusError: On 404 (workflow not found) or 422 (validation error)
        """
        token = await self.get_token(owner, repo)
        headers = {
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github+json",
        }

        payload: dict = {"ref": ref}
        if inputs:
            payload["inputs"] = inputs

        await self._make_request(
            "POST",
            f"https://api.github.com/repos/{owner}/{repo}/actions/workflows/{workflow_file}/dispatches",
            headers=headers,
            json=payload,
        )

        logger.info(
            "workflow_dispatch_triggered",
            owner=owner,
            repo=repo,
            workflow=workflow_file,
            ref=ref,
        )
        return True

    async def get_latest_workflow_run(
        self,
        owner: str,
        repo: str,
        workflow_file: str = "main.yml",
        branch: str = "main",
        created_after: datetime | None = None,
        head_sha: str | None = None,
    ) -> dict | None:
        """Get the most recent workflow run for a branch.

        Args:
            owner: Repository owner
            repo: Repository name
            workflow_file: Workflow filename (e.g. "ci.yml")
            branch: Branch name to filter by
            created_after: If set, ignore runs created before this time.
                Useful to avoid picking up stale runs after a new push.
            head_sha: If set, only return runs for this exact commit SHA.
                Prevents picking up runs from a different commit (e.g. scaffold).

        Returns:
            Workflow run dict with keys: id, status, conclusion, html_url
            None if no runs found (or all runs are stale)
        """
        token = await self.get_token(owner, repo)
        headers = {
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github+json",
        }

        params: dict[str, str | int] = {"branch": branch, "per_page": 1}
        if created_after:
            params["created"] = f">={created_after.strftime('%Y-%m-%dT%H:%M:%SZ')}"
        if head_sha:
            params["head_sha"] = head_sha

        try:
            resp = await self._make_request(
                "GET",
                f"https://api.github.com/repos/{owner}/{repo}/actions/workflows/{workflow_file}/runs",
                headers=headers,
                params=params,
            )
        except httpx.HTTPStatusError as e:
            if e.response.status_code == httpx.codes.NOT_FOUND:
                raise WorkflowNotFoundError(
                    f"Workflow '{workflow_file}' not found in {owner}/{repo}. "
                    "The repository may be missing .github/workflows/ — "
                    "scaffold phase likely failed or was skipped."
                ) from e
            raise

        runs = resp.json().get("workflow_runs", [])
        if not runs:
            return None

        run = runs[0]
        return {
            "id": run["id"],
            "status": run["status"],  # queued, in_progress, completed
            "conclusion": run.get("conclusion"),  # success, failure, cancelled
            "html_url": run["html_url"],
            "created_at": run["created_at"],
            "head_sha": run.get("head_sha"),
        }

    async def wait_for_workflow_completion(
        self,
        owner: str,
        repo: str,
        workflow_file: str = "main.yml",
        branch: str = "main",
        timeout_seconds: int = 600,
        poll_interval: int = 15,
        created_after: datetime | None = None,
        head_sha: str | None = None,
        cancel_check: Callable[[], Awaitable[bool]] | None = None,
    ) -> dict:
        """Wait for the latest workflow run to complete.

        Args:
            owner: Repository owner
            repo: Repository name
            workflow_file: Workflow filename to monitor
            branch: Branch name
            timeout_seconds: Max wait time
            poll_interval: Seconds between polls
            created_after: If set, only consider runs created after this time.
                Prevents picking up stale runs from before a new push.
            head_sha: If set, only consider runs for this exact commit SHA.

        Returns:
            Final workflow run state

        Raises:
            TimeoutError: If workflow doesn't complete within timeout
            RuntimeError: If workflow fails
        """
        start = datetime.now(UTC)

        run: dict | None = None
        try:
            while True:
                elapsed = (datetime.now(UTC) - start).total_seconds()
                if elapsed > timeout_seconds:
                    raise TimeoutError(
                        f"Workflow {workflow_file} did not complete within {timeout_seconds}s"
                    )

                run = await self.get_latest_workflow_run(
                    owner,
                    repo,
                    workflow_file,
                    branch,
                    created_after=created_after,
                    head_sha=head_sha,
                )

                if not run:
                    logger.info("workflow_not_found_waiting", workflow=workflow_file)
                    await asyncio.sleep(poll_interval)
                    continue

                if cancel_check and await cancel_check():
                    # Never returns normally: raises WorkflowCancelledError once the
                    # stop is proven, WorkflowCancellationUnprovenError otherwise.
                    await self._cancel_and_confirm_workflow_run(
                        owner, repo, run["id"], workflow_file, timeout_seconds, poll_interval
                    )

                if run["status"] == "completed":
                    if run["conclusion"] == "success":
                        logger.info(
                            "workflow_completed_success",
                            workflow=workflow_file,
                            run_id=run["id"],
                        )
                        return run
                    try:
                        failure_logs = await self.get_workflow_failure_logs(owner, repo, run["id"])
                    except Exception:
                        failure_logs = "(could not fetch failure details)"
                    raise RuntimeError(
                        f"Workflow {workflow_file} failed: {run['conclusion']}. "
                        f"See: {run['html_url']}\n{failure_logs}"
                    )

                logger.info(
                    "workflow_in_progress",
                    workflow=workflow_file,
                    status=run["status"],
                    elapsed_sec=int(elapsed),
                )
                await asyncio.sleep(poll_interval)
        except asyncio.CancelledError:
            await self._cancel_interrupted_workflow_wait(
                owner,
                repo,
                workflow_file,
                branch,
                created_after,
                head_sha,
                run,
                timeout_seconds,
                poll_interval,
            )
            raise

    async def _cancel_interrupted_workflow_wait(
        self,
        owner: str,
        repo: str,
        workflow_file: str,
        branch: str,
        created_after: datetime | None,
        head_sha: str | None,
        run: dict | None,
        timeout_seconds: int,
        poll_interval: int,
    ) -> None:
        """Cancel and verify a workflow when its awaiting deploy task is interrupted."""
        try:
            if run is None:
                run = await asyncio.shield(
                    self.get_latest_workflow_run(
                        owner,
                        repo,
                        workflow_file,
                        branch,
                        created_after=created_after,
                        head_sha=head_sha,
                    )
                )
            if run is None:
                raise WorkflowCancellationUnprovenError(
                    f"Workflow {workflow_file} cancellation could not identify a run"
                )
            if run["status"] == "completed":
                return
            await asyncio.shield(
                self._cancel_and_confirm_workflow_run(
                    owner, repo, run["id"], workflow_file, timeout_seconds, poll_interval
                )
            )
        except WorkflowCancelledError:
            return
        except WorkflowCancellationUnprovenError:
            raise
        except Exception as exc:
            raise WorkflowCancellationUnprovenError(
                f"Workflow {workflow_file} cancellation could not be verified"
            ) from exc

    async def _cancel_and_confirm_workflow_run(
        self,
        owner: str,
        repo: str,
        run_id: int,
        workflow_file: str,
        timeout_seconds: int,
        poll_interval: int,
    ) -> None:
        """Cancel one known run and prove it reached the cancelled terminal state.

        Single owner of teardown cancellation for both the graceful cancel_check
        path and the interrupted-wait path, so every unproven stop fails closed
        identically.

        Raises:
            WorkflowCancelledError: cancellation is proven terminal.
            WorkflowCancellationUnprovenError: the stop cannot be verified
                (cancel request rejected, wait timed out, or the run completed
                with a non-cancelled conclusion).
        """
        try:
            await self.cancel_workflow_run(owner, repo, run_id)
            await self._wait_for_cancelled_workflow_run(
                owner, repo, run_id, workflow_file, timeout_seconds, poll_interval
            )
        except WorkflowCancelledError:
            raise
        except Exception as exc:
            raise WorkflowCancellationUnprovenError(
                f"Workflow {workflow_file} run {run_id} cancellation could not be verified"
            ) from exc

    async def _wait_for_cancelled_workflow_run(
        self,
        owner: str,
        repo: str,
        run_id: int,
        workflow_file: str,
        timeout_seconds: int,
        poll_interval: int,
    ) -> dict:
        """Wait for GitHub to make a teardown-cancelled workflow terminal."""
        token = await self.get_token(owner, repo)
        headers = {
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github+json",
        }
        start = datetime.now(UTC)
        while True:
            if (datetime.now(UTC) - start).total_seconds() > timeout_seconds:
                raise TimeoutError(
                    f"Workflow {workflow_file} run {run_id} did not cancel within "
                    f"{timeout_seconds}s"
                )
            response = await self._make_request(
                "GET",
                f"https://api.github.com/repos/{owner}/{repo}/actions/runs/{run_id}",
                headers=headers,
            )
            run = response.json()
            if run["status"] == "completed":
                if run.get("conclusion") != "cancelled":
                    raise RuntimeError(
                        f"Workflow {workflow_file} run {run_id} completed as "
                        f"{run.get('conclusion')} after teardown cancellation"
                    )
                logger.info("workflow_cancelled_by_teardown", workflow=workflow_file, run_id=run_id)
                raise WorkflowCancelledError(f"Workflow {workflow_file} cancelled by teardown")
            await asyncio.sleep(poll_interval)

    async def cancel_workflow_run(self, owner: str, repo: str, run_id: int) -> None:
        """Request GitHub Actions to stop one known workflow run."""
        token = await self.get_token(owner, repo)
        try:
            await self._make_request(
                "POST",
                f"https://api.github.com/repos/{owner}/{repo}/actions/runs/{run_id}/cancel",
                headers={
                    "Authorization": f"token {token}",
                    "Accept": "application/vnd.github+json",
                },
            )
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code != httpx.codes.CONFLICT:
                raise

    async def get_workflow_failure_logs(
        self,
        owner: str,
        repo: str,
        run_id: int,
    ) -> str:
        """Fetch failure details from a workflow run.

        Gets failed job names and step names from the GitHub Actions API,
        providing context about what failed in CI.

        Args:
            owner: Repository owner
            repo: Repository name
            run_id: Workflow run ID

        Returns:
            Formatted string describing what failed
        """
        details = await self.get_workflow_failure_details(owner, repo, run_id)
        lines = []
        for job in details["failed_jobs"]:
            lines.append(f"Job '{job['name']}' failed:")
            lines.extend(f"  Step '{step}' failed" for step in job["failed_steps"])

        if not lines:
            return f"Workflow run {run_id} failed (no job details available)"
        return "\n".join(lines)

    async def get_workflow_failure_details(
        self,
        owner: str,
        repo: str,
        run_id: int,
    ) -> dict:
        """Return failed job and step names without downloading raw logs."""
        token = await self.get_token(owner, repo)
        headers = {
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github+json",
        }

        resp = await self._make_request(
            "GET",
            f"https://api.github.com/repos/{owner}/{repo}/actions/runs/{run_id}/jobs",
            headers=headers,
        )
        jobs = resp.json().get("jobs", [])

        failed_jobs = []
        for job in jobs:
            if job.get("conclusion") != "failure":
                continue
            failed_jobs.append(
                {
                    "name": str(job.get("name") or "unnamed job")[:200],
                    "failed_steps": [
                        str(step.get("name") or "unnamed step")[:200]
                        for step in job.get("steps", [])
                        if step.get("conclusion") == "failure"
                    ],
                }
            )
        return {"failed_jobs": failed_jobs, "unavailable_reason": None}

    async def rerun_failed_jobs(self, owner: str, repo: str, run_id: int) -> bool:
        """Rerun only the failed jobs in a workflow run.

        Args:
            owner: Repository owner
            repo: Repository name
            run_id: Workflow run ID

        Returns:
            True if rerun was accepted (201)

        Raises:
            httpx.HTTPStatusError: On 403 (insufficient permissions) or other errors
        """
        token = await self.get_token(owner, repo)
        headers = {
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github+json",
        }

        await self._make_request(
            "POST",
            f"https://api.github.com/repos/{owner}/{repo}/actions/runs/{run_id}/rerun-failed-jobs",
            headers=headers,
        )

        logger.info(
            "workflow_rerun_failed_jobs_triggered",
            owner=owner,
            repo=repo,
            run_id=run_id,
        )
        return True

    async def wait_for_run_completion(
        self,
        owner: str,
        repo: str,
        run_id: int,
        timeout_seconds: int = 600,
        poll_interval: int = 15,
    ) -> dict:
        """Wait for a specific workflow run to complete.

        Unlike wait_for_workflow_completion, this polls a known run_id directly.
        Useful after rerun_failed_jobs where the run_id stays the same but
        created_at doesn't change (so the created_after filter won't find it).

        Args:
            owner: Repository owner
            repo: Repository name
            run_id: Workflow run ID to poll
            timeout_seconds: Max wait time
            poll_interval: Seconds between polls

        Returns:
            Dict with {id, status, conclusion, html_url} on success

        Raises:
            RuntimeError: If run completes with non-success conclusion
            TimeoutError: If run doesn't complete within timeout
        """
        token = await self.get_token(owner, repo)
        headers = {
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github+json",
        }

        start = datetime.now(UTC)

        while True:
            elapsed = (datetime.now(UTC) - start).total_seconds()
            if elapsed > timeout_seconds:
                raise TimeoutError(
                    f"Workflow run {run_id} did not complete within {timeout_seconds}s"
                )

            resp = await self._make_request(
                "GET",
                f"https://api.github.com/repos/{owner}/{repo}/actions/runs/{run_id}",
                headers=headers,
            )
            run = resp.json()

            if run["status"] == "completed":
                result = {
                    "id": run["id"],
                    "status": run["status"],
                    "conclusion": run.get("conclusion"),
                    "html_url": run["html_url"],
                }
                if run.get("conclusion") == "success":
                    logger.info(
                        "workflow_run_completed_success",
                        run_id=run_id,
                    )
                    return result
                else:
                    # Fetch failure details for better error context
                    try:
                        failure_logs = await self.get_workflow_failure_logs(owner, repo, run_id)
                    except Exception:
                        failure_logs = "(could not fetch failure details)"
                    raise RuntimeError(
                        f"Workflow run {run_id} failed: {run.get('conclusion')}. "
                        f"See: {run['html_url']}\n{failure_logs}"
                    )

            logger.info(
                "workflow_run_in_progress",
                run_id=run_id,
                status=run["status"],
                elapsed_sec=int(elapsed),
            )
            await asyncio.sleep(poll_interval)
