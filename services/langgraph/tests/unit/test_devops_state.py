"""Tests for DevOps subgraph state."""

from src.subgraphs.devops.state import DevOpsState


class TestDevOpsState:
    def test_smoke_result_field_declared(self):
        """smoke_result must be a declared field in DevOpsState."""
        annotations = DevOpsState.__annotations__
        assert "smoke_result" in annotations, "smoke_result field missing from DevOpsState"

    def test_smoke_result_accepts_dict(self):
        """DevOpsState should accept smoke_result as a dict."""
        state: DevOpsState = {
            "messages": [],
            "project_id": None,
            "project_spec": None,
            "allocated_resources": {},
            "repo_info": None,
            "provided_secrets": {},
            "resolved_secrets": {},
            "missing_user_secrets": [],
            "deployment_result": None,
            "deployed_url": None,
            "errors": [],
            "smoke_result": {
                "status": "pass",
                "checks": [
                    {"module": "backend", "result": "pass", "detail": "HTTP 200"},
                ],
            },
        }
        assert state["smoke_result"]["status"] == "pass"

    def test_smoke_result_accepts_none(self):
        """DevOpsState should accept smoke_result as None."""
        state: DevOpsState = {
            "messages": [],
            "project_id": None,
            "project_spec": None,
            "allocated_resources": {},
            "repo_info": None,
            "provided_secrets": {},
            "resolved_secrets": {},
            "missing_user_secrets": [],
            "deployment_result": None,
            "deployed_url": None,
            "errors": [],
            "smoke_result": None,
        }
        assert state["smoke_result"] is None
