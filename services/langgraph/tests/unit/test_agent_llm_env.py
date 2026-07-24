"""Keep the agent LLM env groups, Settings, and .env.example in sync.

A missing var here is invisible at runtime: the agent just never consumes its
queue, so drift between the three has to fail in tests instead.
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.config.agent_llm_env import AGENT_LLM_ENV, missing_llm_env
from src.config.settings import Settings

REPO_ROOT = Path(__file__).resolve().parents[4]
ENV_EXAMPLE = REPO_ROOT / ".env.example"


def _env_example_keys() -> set[str]:
    keys = set()
    for line in ENV_EXAMPLE.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        keys.add(line.split("=", 1)[0].strip())
    return keys


class TestEnvGroupsMatchSettings:
    @pytest.mark.parametrize("agent", sorted(AGENT_LLM_ENV))
    def test_every_documented_var_is_read_by_settings(self, agent):
        for name in AGENT_LLM_ENV[agent]:
            assert name.lower() in Settings.model_fields, f"{name} is not read by Settings"

    def test_every_llm_settings_field_belongs_to_a_group(self):
        """A new agent's *_LLM_* field must join a group, not sit undocumented."""
        declared = {name.lower() for group in AGENT_LLM_ENV.values() for name in group}
        llm_fields = {
            field
            for field in Settings.model_fields
            if field.endswith(("_llm_model", "_llm_base_url", "_llm_api_key"))
        }
        assert llm_fields == declared


class TestEnvExampleDocumentsGroups:
    @pytest.mark.parametrize("agent", sorted(AGENT_LLM_ENV))
    def test_group_is_present_in_env_example(self, agent):
        documented = _env_example_keys()
        for name in AGENT_LLM_ENV[agent]:
            assert name in documented, f"{name} missing from .env.example"

    @pytest.mark.parametrize("agent", sorted(AGENT_LLM_ENV))
    def test_group_is_explained_in_env_example(self, agent):
        """Each group carries a comment saying the agent won't work without it."""
        text = ENV_EXAMPLE.read_text()
        first_var = AGENT_LLM_ENV[agent][0]
        comment_block = text.split(f"\n{first_var}=", 1)[0].rsplit("\n\n", 1)[-1]
        assert "required" in comment_block.lower()


class TestArchitectStartupGuard:
    def test_refuses_to_start_without_config(self):
        from src.consumers import architect

        settings = MagicMock(
            architect_llm_model=None,
            architect_llm_base_url=None,
            architect_llm_api_key=None,
        )
        with (
            patch.object(architect, "get_settings", return_value=settings),
            patch.object(architect, "start_worker") as start_worker,
        ):
            with pytest.raises(RuntimeError) as exc:
                architect.main()

        start_worker.assert_not_called()
        assert "ARCHITECT_LLM_API_KEY" in str(exc.value)

    def test_starts_when_configured(self):
        from src.consumers import architect

        settings = MagicMock(
            architect_llm_model="m",
            architect_llm_base_url="u",
            architect_llm_api_key="k",
        )
        with (
            patch.object(architect, "get_settings", return_value=settings),
            patch.object(architect, "start_worker") as start_worker,
        ):
            architect.main()

        start_worker.assert_called_once()


class TestMissingLlmEnv:
    def test_reports_only_unset_vars(self):
        settings = MagicMock(
            architect_llm_model="m",
            architect_llm_base_url="",
            architect_llm_api_key=None,
        )

        assert missing_llm_env("architect", settings) == [
            "ARCHITECT_LLM_BASE_URL",
            "ARCHITECT_LLM_API_KEY",
        ]

    def test_empty_when_fully_configured(self):
        settings = MagicMock(po_llm_model="m", po_llm_base_url="u", po_llm_api_key="k")

        assert missing_llm_env("po", settings) == []
