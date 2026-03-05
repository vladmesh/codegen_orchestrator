"""Tests that _get_user_id raises RuntimeError when ORCHESTRATOR_USER_ID is not set."""

import os
from unittest.mock import patch

import pytest


@pytest.mark.parametrize(
    "module_path",
    [
        "orchestrator_cli.commands.engineering",
        "orchestrator_cli.commands.deploy",
        "orchestrator_cli.commands.respond",
    ],
)
class TestGetUserIdFailsFast:
    def test_raises_when_not_set(self, module_path):
        import importlib

        mod = importlib.import_module(module_path)
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ORCHESTRATOR_USER_ID", None)
            with pytest.raises(RuntimeError, match="ORCHESTRATOR_USER_ID"):
                mod._get_user_id()

    def test_returns_value_when_set(self, module_path):
        import importlib

        mod = importlib.import_module(module_path)
        with patch.dict(os.environ, {"ORCHESTRATOR_USER_ID": "user-42"}):
            assert mod._get_user_id() == "user-42"
