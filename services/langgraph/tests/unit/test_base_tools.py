"""Unit tests for Base Tools."""

from src.capabilities.base import (
    BASE_TOOLS,
    finish_task,
    request_capabilities,
)
from src.state.context import get_current_state, set_tool_context


class TestBaseTools:
    """Tests for base tools module."""

    def test_base_tools_list(self):
        """Test that BASE_TOOLS contains expected tools."""
        tool_names = {t.name for t in BASE_TOOLS}
        expected = {"respond_to_user", "search_knowledge", "request_capabilities", "finish_task"}
        assert tool_names == expected

    def test_all_tools_have_name(self):
        """Test that all base tools have a name attribute."""
        for tool in BASE_TOOLS:
            assert hasattr(tool, "name")
            assert isinstance(tool.name, str)


class TestSetToolContext:
    """Tests for set_tool_context function."""

    def test_set_and_get_state(self):
        """Test setting and getting state context."""
        test_state = {
            "telegram_user_id": 123,
            "thread_id": "user_123_1",
            "current_project": "test-project",
        }

        set_tool_context(test_state)
        result = get_current_state()

        assert result == test_state


class TestRequestCapabilities:
    """Tests for request_capabilities tool."""

    def test_valid_capabilities(self):
        """Test requesting valid capabilities."""
        set_tool_context({"active_capabilities": []})

        result = request_capabilities.invoke(
            {
                "capabilities": ["deploy", "infrastructure"],
                "reason": "Testing",
            }
        )

        assert "enabled" in result
        assert set(result["enabled"]) == {"deploy", "infrastructure"}
        assert "new_tools" in result
        assert len(result["new_tools"]) > 0

    def test_invalid_capability(self):
        """Test requesting invalid capability."""
        set_tool_context({"active_capabilities": []})

        result = request_capabilities.invoke(
            {
                "capabilities": ["nonexistent"],
                "reason": "Testing",
            }
        )

        assert "error" in result
        assert "available" in result

    def test_merge_with_existing(self):
        """Test merging with existing capabilities."""
        set_tool_context({"active_capabilities": ["project_management"]})

        result = request_capabilities.invoke(
            {
                "capabilities": ["deploy"],
                "reason": "Testing",
            }
        )

        enabled = set(result["enabled"])
        assert "project_management" in enabled
        assert "deploy" in enabled


class TestFinishTask:
    """Tests for finish_task tool."""

    def test_finish_task_returns_summary(self):
        """Test that finish_task returns correct structure."""
        set_tool_context(
            {
                "thread_id": "user_123_5",
                "telegram_user_id": 123,
            }
        )

        result = finish_task.invoke({"summary": "Task completed successfully"})

        assert result["finished"] is True
        assert result["thread_id"] == "user_123_5"
        assert result["summary"] == "Task completed successfully"
