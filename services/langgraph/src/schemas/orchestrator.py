"""Pydantic schemas for OrchestratorState components.

These schemas define the structure of data passed between agents
in the LangGraph orchestrator. Using these schemas:
1. Provides IDE autocomplete for developers
2. Validates data at runtime (when explicitly validated)
3. Documents expected fields for each state component

Note: OrchestratorState itself remains a TypedDict for LangGraph compatibility,
but these schemas document and validate the nested dict values.
"""

from typing import Literal

from pydantic import BaseModel, Field


class RepoInfo(BaseModel):
    """Repository information stored in orchestrator state.

    Created by the Architect agent after creating a GitHub repository.
    Used by DevOps for deployment.
    """

    full_name: str = Field(..., description="Full repo name: 'owner/repo'")
    html_url: str = Field(..., description="Web URL: 'https://github.com/owner/repo'")
    clone_url: str = Field(..., description="Clone URL: 'https://github.com/owner/repo.git'")

    # Optional fields (may be added later in the flow)
    default_branch: str = Field("main", description="Default branch name")
    ssh_url: str | None = Field(None, description="SSH clone URL if available")


class AllocatedResource(BaseModel):
    """Allocated server/port resource for a service.

    Created by Zavhoz agent when allocating resources.
    Keys in allocated_resources dict are typically 'server_handle:port' or service name.
    """

    server_handle: str = Field(..., description="Server handle (e.g., 'vps-267179')")
    server_ip: str = Field(..., description="Server public IP address")
    port: int = Field(..., description="Allocated port number")
    service_name: str = Field(..., description="Name of the service using this resource")
    project_id: str | None = Field(None, description="Associated project ID")


class ProjectIntent(BaseModel):
    """Intent classification from Product Owner.

    Describes what the user wants to do, which determines the flow through the graph.
    """

    intent: Literal["new_project", "maintenance", "deploy", "update_project"] = Field(
        ..., description="Intent type determining graph routing"
    )
    summary: str | None = Field(None, description="Brief summary of the user's request")
    project_id: str | None = Field(None, description="Project ID if working with existing project")


class ProvisioningResult(BaseModel):
    """Result from provisioner node.

    Contains status and details about server provisioning operation.
    """

    status: Literal["success", "failed", "skipped"] = Field(..., description="Provisioning status")
    server_handle: str | None = Field(None, description="Server that was provisioned")
    server_ip: str | None = Field(None, description="Server IP address")
    method: Literal["reinstall", "existing_access", "password_reset"] | None = Field(
        None, description="Provisioning method used"
    )
    services_redeployed: int = Field(0, description="Number of services redeployed after recovery")
    services_failed: int = Field(0, description="Number of services that failed to redeploy")
    error_message: str | None = Field(None, description="Error details if failed")


class TestResults(BaseModel):
    """Test execution results from the testing phase.

    Populated by the Tester agent/worker after running tests.
    """

    passed: int = Field(0, description="Number of passed tests")
    failed: int = Field(0, description="Number of failed tests")
    skipped: int = Field(0, description="Number of skipped tests")
    errors: int = Field(0, description="Number of test errors")
    total: int = Field(0, description="Total test count")
    output: str = Field("", description="Test output/logs (truncated)")
    duration_seconds: float = Field(0.0, description="Total test duration")

    @property
    def success(self) -> bool:
        """Check if all tests passed."""
        return self.failed == 0 and self.errors == 0


class EngineeringState(BaseModel):
    """Engineering subgraph state tracking.

    Tracks progress through the Architect → Developer → Tester loop.
    """

    status: Literal["idle", "working", "done", "blocked"] = Field(
        "idle", description="Current engineering status"
    )
    iteration_count: int = Field(0, description="Number of review/fix iterations")
    max_iterations: int = Field(3, description="Maximum allowed iterations")
    review_feedback: str | None = Field(None, description="Feedback from code review")
    needs_human_approval: bool = Field(False, description="Whether human approval is required")
    human_approval_reason: str | None = Field(None, description="Reason for human approval request")
