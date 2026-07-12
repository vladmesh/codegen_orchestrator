"""Typed result payloads stored in `Run.result`, keyed by `RunType`.

Each `RunType` has exactly one result shape. The models use `extra="forbid"`
so an unknown field or a payload belonging to another run type is rejected at
the boundary instead of being silently carried as a raw dict. Unknown enum
values (e.g. an outcome string the code doesn't know) fail validation for the
same reason. `RunDTO` binds the union to `RunType` and rejects a mismatched
pair — see `RunDTO._check_result_matches_type`.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from shared.contracts.dto.engineering import EngineeringStatus
from shared.contracts.queues.deploy import DeployAction, DeployOutcome
from shared.contracts.queues.qa import QAOutcome


class EngineeringRunResult(BaseModel):
    """Result of an engineering run (written by the engineering result handler)."""

    model_config = ConfigDict(extra="forbid")

    engineering_status: EngineeringStatus
    commit_sha: str | None = None
    selected_modules: list[str] | None = None
    test_results: dict | None = None


class DeployRunResult(BaseModel):
    """Result of a deploy run.

    `deploy_outcome` is the routing field the scheduler reads. `deployment_result`
    and `smoke_result` are opaque diagnostic blobs from the DevOps subgraph; they
    are stored for observability and never routed on.
    """

    model_config = ConfigDict(extra="forbid")

    deploy_outcome: DeployOutcome
    deployed_url: str | None = None
    application_id: int | None = None
    bot_username: str | None = None
    deploy_fix_attempt: int = 0
    error_details: str | None = None
    action: DeployAction | None = None
    deployment_result: dict | None = None
    smoke_result: dict | None = None


class QAFailedCheck(BaseModel):
    """A single failed QA check the scheduler turns into a fix-task line."""

    model_config = ConfigDict(extra="forbid")

    name: str
    detail: str


class QARunResult(BaseModel):
    """Result of a QA run (written by the QA consumer)."""

    model_config = ConfigDict(extra="forbid")

    qa_outcome: QAOutcome
    summary: str | None = None
    failed_checks: list[QAFailedCheck] = Field(default_factory=list)
    report: str | None = None
    qa_attempt: int | None = None
    deployed_url: str | None = None
    error: str | None = None


RunResult = EngineeringRunResult | DeployRunResult | QARunResult
