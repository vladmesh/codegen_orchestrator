"""Tests for project tools."""

import pytest

from src.tools.projects import validate_project_name


class TestValidateProjectName:
    """Tests for project name validation."""

    def test_valid_simple_name(self):
        """Valid simple lowercase name passes."""
        validate_project_name("myproject")

    def test_valid_with_numbers(self):
        """Valid name with numbers passes."""
        validate_project_name("project123")

    def test_valid_with_hyphens(self):
        """Valid name with hyphens passes."""
        validate_project_name("my-cool-project")

    def test_valid_complex(self):
        """Valid complex name passes."""
        validate_project_name("weather-bot-v2")

    def test_invalid_uppercase(self):
        """Uppercase letters are rejected."""
        with pytest.raises(ValueError, match="lowercase"):
            validate_project_name("MyProject")

    def test_invalid_starts_with_number(self):
        """Names starting with number are rejected."""
        with pytest.raises(ValueError, match="start with a letter"):
            validate_project_name("123project")

    def test_invalid_underscores(self):
        """Underscores are rejected (not kebab-case)."""
        with pytest.raises(ValueError, match="letters, numbers, and hyphens"):
            validate_project_name("my_project")

    def test_invalid_spaces(self):
        """Spaces are rejected."""
        with pytest.raises(ValueError, match="letters, numbers, and hyphens"):
            validate_project_name("my project")

    def test_invalid_special_chars(self):
        """Special characters are rejected."""
        with pytest.raises(ValueError, match="letters, numbers, and hyphens"):
            validate_project_name("my@project!")

    def test_invalid_cyrillic(self):
        """Cyrillic characters are rejected."""
        with pytest.raises(ValueError, match="letters, numbers, and hyphens"):
            validate_project_name("мойпроект")

    def test_invalid_empty(self):
        """Empty string is rejected."""
        with pytest.raises(ValueError):
            validate_project_name("")

    def test_invalid_starts_with_hyphen(self):
        """Names starting with hyphen are rejected."""
        with pytest.raises(ValueError, match="start with a letter"):
            validate_project_name("-myproject")
