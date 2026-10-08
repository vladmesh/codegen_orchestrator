"""An operator's approval to deploy a story's repaired default-branch head.

A merged story whose commit never got images (`images_not_published`) cannot be
deployed at that commit: its CI run is over. Once the product's CI is repaired
on the default branch, an administrator approves a later commit of that branch
through `POST /api/stories/{id}/deploy-repaired-head`. The approval is written by
that endpoint alone, into the story's App-authenticated record
(`Story.generated_product_timeline`), and the merged-PR poller deploys the
approved commit instead of the merge commit — through the same path, so the
CREATE/FEATURE choice, the initial-owner seed and the deploy handler are the
ordinary ones.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field

#: The timeline key that holds the approval. The story PATCH refuses it from any
#: caller but an internal service, the way it refuses `deploy_observation`.
REPAIRED_HEAD_APPROVAL_KEY = "repaired_head_deploy_approval"

GitSha = Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")]


class RepairedHeadDeployCommand(BaseModel):
    """The operator's request: which stop it releases and which commit to deploy."""

    model_config = ConfigDict(extra="forbid")

    stop_id: str = Field(min_length=1)
    deployed_commit_sha: GitSha


class RepairedHeadDeployApproval(BaseModel):
    """The recorded approval, bound to one merged pull request.

    ``merge_commit_sha`` and ``pr_number`` say which merge it repairs, so a later
    PR of the same story never inherits it. ``superseded_commit_sha`` is the
    commit the refusal named — the merge commit, or an earlier approved commit
    whose images did not appear either.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    actor: str
    approved_at: datetime
    pr_number: int
    head_sha: GitSha
    merge_commit_sha: GitSha
    approved_commit_sha: GitSha
    superseded_commit_sha: GitSha
    quarantine_reason: dict[str, Any]


def approval_for_merge(
    timeline: object, *, pr_number: int | None, merge_commit_sha: str | None
) -> RepairedHeadDeployApproval | None:
    """The approval that repairs exactly this merge, or None.

    An approval recorded for another pull request or another merge is history,
    not an instruction, and is ignored.
    """
    if not isinstance(timeline, dict) or REPAIRED_HEAD_APPROVAL_KEY not in timeline:
        return None
    approval = RepairedHeadDeployApproval.model_validate(timeline[REPAIRED_HEAD_APPROVAL_KEY])
    if approval.pr_number != pr_number or approval.merge_commit_sha != merge_commit_sha:
        return None
    return approval
