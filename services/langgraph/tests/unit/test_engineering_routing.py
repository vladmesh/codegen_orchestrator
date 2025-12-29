"""Tests for engineering subgraph routing logic."""

from langgraph.graph import END

from src.subgraphs.engineering import route_after_preparer


class TestRouteAfterPreparer:
    """Tests for route_after_preparer function."""

    def test_routes_to_developer_when_prepared_simple(self):
        """Developer is called even for simple projects."""
        state = {
            "repo_prepared": True,
            "project_complexity": "simple",
        }
        result = route_after_preparer(state)
        assert result == "developer"

    def test_routes_to_developer_when_prepared_complex(self):
        """Developer is called for complex projects."""
        state = {
            "repo_prepared": True,
            "project_complexity": "complex",
        }
        result = route_after_preparer(state)
        assert result == "developer"

    def test_routes_to_developer_when_complexity_not_set(self):
        """Developer is called when complexity is not set."""
        state = {
            "repo_prepared": True,
        }
        result = route_after_preparer(state)
        assert result == "developer"

    def test_routes_to_end_when_not_prepared(self):
        """END is returned when repo is not prepared."""
        state = {
            "repo_prepared": False,
            "project_complexity": "simple",
        }
        result = route_after_preparer(state)
        assert result == END

    def test_routes_to_end_when_prepared_is_none(self):
        """END is returned when repo_prepared is None."""
        state = {
            "repo_prepared": None,
        }
        result = route_after_preparer(state)
        assert result == END

    def test_routes_to_end_when_prepared_missing(self):
        """END is returned when repo_prepared key is missing."""
        state = {}
        result = route_after_preparer(state)
        assert result == END
