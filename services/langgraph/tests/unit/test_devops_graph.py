"""Unit tests for DevOps subgraph topology."""

from unittest.mock import AsyncMock, patch

from langgraph.graph import END, START, StateGraph
import pytest

from src.subgraphs.devops.graph import (
    create_devops_subgraph,
    resolve_secrets,
    route_after_deployer,
    route_after_secret_resolver,
)
from src.subgraphs.devops.state import DevOpsState


class TestDevOpsGraphTopology:
    """Verify graph contains smoke_tester node and correct routing."""

    def test_smoke_tester_node_exists(self):
        """smoke_tester should be a node in the compiled graph."""
        graph = create_devops_subgraph()
        node_names = set(graph.get_graph().nodes.keys())
        assert "smoke_tester" in node_names

    def test_deployer_connects_to_smoke_tester(self):
        """deployer should route to smoke_tester (not directly to END)."""
        graph = create_devops_subgraph()
        edges = graph.get_graph().edges
        # deployer should have conditional edges, one of which goes to smoke_tester
        deployer_targets = {e.target for e in edges if e.source == "deployer"}
        assert "smoke_tester" in deployer_targets

    def test_smoke_tester_connects_to_end(self):
        """smoke_tester should connect to __end__."""
        graph = create_devops_subgraph()
        edges = graph.get_graph().edges
        smoke_targets = {e.target for e in edges if e.source == "smoke_tester"}
        assert "__end__" in smoke_targets


class TestRouteAfterDeployer:
    """Verify routing logic after deployer node."""

    def test_routes_to_smoke_when_deployed(self):
        """Should route to smoke_tester when deployed_url is set and no errors."""
        state = {"deployed_url": "http://1.2.3.4:8000", "errors": []}
        assert route_after_deployer(state) == "smoke_tester"

    def test_skips_smoke_on_errors(self):
        """Should skip smoke_tester when deployer set errors."""
        state = {"deployed_url": None, "errors": ["Deploy failed"]}
        assert route_after_deployer(state) == END

    def test_skips_smoke_when_no_url(self):
        """Should skip smoke_tester when no deployed_url."""
        state = {"deployed_url": None, "errors": []}
        assert route_after_deployer(state) == END


class TestRouteAfterSecretResolver:
    """Resolver errors must stop the graph before deployment side effects."""

    def test_stops_before_deployer_on_resolution_error(self):
        state = {"errors": ["project_id is required for secret resolution"]}

        assert route_after_secret_resolver(state) == END

    @pytest.mark.asyncio
    async def test_typed_resolver_error_is_returned_to_deploy_result_path(self):
        """A resolver failure remains visible without running downstream deployment."""
        result = await resolve_secrets(
            {"project_id": None, "project_spec": {"slug": "test-0000"}}
        )

        assert result == {"errors": ["project_id is required for secret resolution"]}


class TestSmokeResultPropagation:
    """Verify smoke_result survives ainvoke() — the #25 regression."""

    @pytest.mark.asyncio
    async def test_smoke_result_in_ainvoke_output(self):
        """Build deployer_stub → smoke_tester mini-graph, check ainvoke returns smoke_result."""

        async def deployer_stub(state: DevOpsState) -> dict:
            return {
                "deployment_result": {"status": "success"},
                "deployed_url": "http://1.2.3.4:8000",
            }

        mock_response = AsyncMock()
        mock_response.status_code = 200

        mock_http = AsyncMock()
        mock_http.__aenter__ = AsyncMock(return_value=mock_http)
        mock_http.__aexit__ = AsyncMock(return_value=False)
        mock_http.get = AsyncMock(return_value=mock_response)

        from src.subgraphs.devops.smoke import smoke_tester_node

        graph = StateGraph(DevOpsState)
        graph.add_node("deployer", deployer_stub)
        graph.add_node("smoke_tester", smoke_tester_node.run)
        graph.add_edge(START, "deployer")
        graph.add_edge("deployer", "smoke_tester")
        graph.add_edge("smoke_tester", END)
        compiled = graph.compile()

        input_state = {
            "messages": [],
            "project_id": "test-proj",
            "project_spec": {"slug": "test-proj-0000", "config": {"modules": ["backend"]}},
            "allocated_resources": {
                "srv:8000": {
                    "server_ip": "1.2.3.4",
                    "port": 8000,
                    "service_name": "backend",
                }
            },
            "repo_info": None,
            "provided_secrets": {},
            "missing_user_secrets": [],
            "deployment_result": None,
            "deployed_url": None,
            "smoke_result": None,
            "errors": [],
        }

        with patch("src.subgraphs.devops.smoke.httpx.AsyncClient", return_value=mock_http):
            result = await compiled.ainvoke(input_state)

        assert "smoke_result" in result, "smoke_result missing from ainvoke() output"
        assert result["smoke_result"] is not None, "smoke_result is None"
        assert result["smoke_result"]["status"] == "pass"
        assert result["smoke_result"]["checks"][0]["module"] == "backend"
        assert result["smoke_result"]["checks"][0]["result"] == "pass"
