"""Unit tests for architect tools."""

from src.tools.architect_tools import (
    AVAILABLE_MODULES,
    customize_task_instructions,
    select_modules,
    set_deployment_hints,
    set_project_complexity,
)


class TestSelectModules:
    """Tests for select_modules tool."""

    def test_valid_single_module(self):
        """Test selecting a single valid module."""
        result = select_modules.invoke({"modules": ["backend"]})
        assert "Selected modules" in result
        assert "backend" in result

    def test_valid_multiple_modules(self):
        """Test selecting multiple valid modules."""
        result = select_modules.invoke({"modules": ["backend", "tg_bot"]})
        assert "Selected modules" in result
        assert "backend" in result
        assert "tg_bot" in result

    def test_all_available_modules(self):
        """Test selecting all available modules."""
        result = select_modules.invoke({"modules": AVAILABLE_MODULES})
        assert "Selected modules" in result
        for module in AVAILABLE_MODULES:
            assert module in result

    def test_invalid_module(self):
        """Test error on invalid module."""
        result = select_modules.invoke({"modules": ["backend", "invalid_module"]})
        assert "Error" in result
        assert "invalid_module" in result

    def test_empty_modules(self):
        """Test error on empty modules list."""
        result = select_modules.invoke({"modules": []})
        assert "Error" in result
        assert "At least one module" in result

    def test_only_invalid_modules(self):
        """Test error when all modules are invalid."""
        result = select_modules.invoke({"modules": ["foo", "bar"]})
        assert "Error" in result
        assert "foo" in result
        assert "bar" in result


class TestSetDeploymentHints:
    """Tests for set_deployment_hints tool."""

    def test_default_values(self):
        """Test with all default values."""
        result = set_deployment_hints.invoke({})
        assert "Deployment hints saved" in result
        assert "8000" in result  # default backend_port
        assert "4321" in result  # default frontend_port

    def test_custom_domain(self):
        """Test with custom domain."""
        result = set_deployment_hints.invoke({"domain": "myapp.example.com"})
        assert "Deployment hints saved" in result
        assert "myapp.example.com" in result

    def test_custom_ports(self):
        """Test with custom ports."""
        result = set_deployment_hints.invoke(
            {
                "backend_port": 9000,
                "frontend_port": 3000,
            }
        )
        assert "Deployment hints saved" in result
        assert "9000" in result
        assert "3000" in result

    def test_ssl_disabled(self):
        """Test with SSL disabled."""
        result = set_deployment_hints.invoke({"needs_ssl": False})
        assert "Deployment hints saved" in result
        assert "False" in result

    def test_environment_vars(self):
        """Test with environment variables list."""
        result = set_deployment_hints.invoke({"environment_vars": ["TELEGRAM_TOKEN", "OPENAI_KEY"]})
        assert "Deployment hints saved" in result
        assert "TELEGRAM_TOKEN" in result
        assert "OPENAI_KEY" in result


class TestCustomizeTaskInstructions:
    """Tests for customize_task_instructions tool."""

    def test_valid_instructions(self):
        """Test with valid instructions."""
        result = customize_task_instructions.invoke(
            {"instructions": "Use Redis for caching API responses"}
        )
        assert "Custom instructions saved" in result
        assert "chars" in result

    def test_long_instructions(self):
        """Test with long instructions."""
        long_text = "x" * 1000
        result = customize_task_instructions.invoke({"instructions": long_text})
        assert "Custom instructions saved" in result
        assert "1000 chars" in result

    def test_empty_instructions(self):
        """Test error on empty instructions."""
        result = customize_task_instructions.invoke({"instructions": ""})
        assert "Error" in result
        assert "cannot be empty" in result

    def test_whitespace_only_instructions(self):
        """Test error on whitespace-only instructions."""
        result = customize_task_instructions.invoke({"instructions": "   "})
        assert "Error" in result
        assert "cannot be empty" in result


class TestSetProjectComplexity:
    """Tests for set_project_complexity tool."""

    def test_simple_complexity(self):
        """Test setting simple complexity."""
        result = set_project_complexity.invoke({"complexity": "simple"})
        assert "simple" in result
        assert "Error" not in result

    def test_complex_complexity(self):
        """Test setting complex complexity."""
        result = set_project_complexity.invoke({"complexity": "complex"})
        assert "complex" in result
        assert "Error" not in result

    def test_invalid_complexity(self):
        """Test error on invalid complexity."""
        result = set_project_complexity.invoke({"complexity": "medium"})
        assert "Error" in result
        assert "medium" in result

    def test_empty_complexity(self):
        """Test error on empty complexity."""
        result = set_project_complexity.invoke({"complexity": ""})
        assert "Error" in result
