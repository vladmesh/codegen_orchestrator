"""Unit tests for DevOps subgraph topology."""

from langgraph.graph import END

from src.subgraphs.devops.graph import (
    create_devops_subgraph,
    route_after_deployer,
)


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
