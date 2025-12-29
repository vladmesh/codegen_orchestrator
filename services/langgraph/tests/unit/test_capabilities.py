"""Unit tests for Capability Registry."""

from src.capabilities import (
    CAPABILITY_REGISTRY,
    TOOLS_MAP,
    get_tools_for_capabilities,
    list_available_capabilities,
)
from src.capabilities.base import BASE_TOOLS


class TestCapabilityRegistry:
    """Tests for capability registry functions."""

    def test_capability_registry_has_expected_capabilities(self):
        """Test that all expected capabilities are defined."""
        expected_caps = {
            "deploy",
            "infrastructure",
            "project_management",
            "engineering",
            "diagnose",
            "admin",
        }
        assert set(CAPABILITY_REGISTRY.keys()) == expected_caps

    def test_capability_registry_has_descriptions(self):
        """Test that each capability has a description."""
        for name, cap in CAPABILITY_REGISTRY.items():
            assert "description" in cap, f"Capability {name} missing description"
            assert isinstance(cap["description"], str)
            assert len(cap["description"]) > 0

    def test_capability_registry_has_tools_list(self):
        """Test that each capability has a tools list."""
        for name, cap in CAPABILITY_REGISTRY.items():
            assert "tools" in cap, f"Capability {name} missing tools"
            assert isinstance(cap["tools"], list)

    def test_tools_map_contains_capability_tools(self):
        """Test that TOOLS_MAP contains all tools referenced in capabilities."""
        for cap_name, cap in CAPABILITY_REGISTRY.items():
            for tool_name in cap["tools"]:
                assert tool_name in TOOLS_MAP, f"Tool {tool_name} from {cap_name} not in TOOLS_MAP"


class TestGetToolsForCapabilities:
    """Tests for get_tools_for_capabilities function."""

    def test_always_includes_base_tools(self):
        """Test that base tools are always included."""
        tools = get_tools_for_capabilities([])
        tool_names = {t.name for t in tools}

        for base_tool in BASE_TOOLS:
            assert base_tool.name in tool_names

    def test_includes_capability_tools(self):
        """Test that requested capability tools are included."""
        tools = get_tools_for_capabilities(["project_management"])
        tool_names = {t.name for t in tools}

        assert "list_projects" in tool_names
        assert "get_project_status" in tool_names
        assert "create_project_intent" in tool_names

    def test_multiple_capabilities(self):
        """Test loading multiple capabilities."""
        tools = get_tools_for_capabilities(["infrastructure", "deploy"])
        tool_names = {t.name for t in tools}

        # Infrastructure tools
        assert "list_managed_servers" in tool_names
        assert "find_suitable_server" in tool_names

        # Deploy tools
        assert "check_ready_to_deploy" in tool_names
        assert "activate_project" in tool_names

    def test_unknown_capability_ignored(self):
        """Test that unknown capabilities are silently ignored."""
        tools = get_tools_for_capabilities(["unknown_capability", "project_management"])
        tool_names = {t.name for t in tools}

        # Should still have project_management tools
        assert "list_projects" in tool_names

    def test_no_duplicate_tools(self):
        """Test that tools are not duplicated."""
        tools = get_tools_for_capabilities(["deploy", "infrastructure"])
        tool_names = [t.name for t in tools]

        # Check no duplicates
        assert len(tool_names) == len(set(tool_names))


class TestListAvailableCapabilities:
    """Tests for list_available_capabilities function."""

    def test_returns_dict(self):
        """Test that it returns a dict."""
        result = list_available_capabilities()
        assert isinstance(result, dict)

    def test_contains_all_capabilities(self):
        """Test that all capabilities are included."""
        result = list_available_capabilities()
        assert set(result.keys()) == set(CAPABILITY_REGISTRY.keys())

    def test_values_are_descriptions(self):
        """Test that values are capability descriptions."""
        result = list_available_capabilities()
        for name, desc in result.items():
            assert desc == CAPABILITY_REGISTRY[name]["description"]
