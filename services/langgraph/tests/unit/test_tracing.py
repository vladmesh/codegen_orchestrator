"""Tests for Langfuse tracing utility."""

import importlib
import os
from unittest.mock import patch

from src.tracing import build_langfuse_metadata


def _clean_env_no_langfuse():
    """Return current env without any LANGFUSE_* vars."""
    return {k: v for k, v in os.environ.items() if not k.startswith("LANGFUSE_")}


def _reload_and_call():
    """Reimport tracing module and call get_langfuse_callbacks (avoids module cache)."""
    import src.tracing as mod

    importlib.reload(mod)
    return mod.get_langfuse_callbacks()


class TestGetLangfuseCallbacks:
    def test_returns_handler_when_all_env_vars_set(self):
        env = {
            **_clean_env_no_langfuse(),
            "LANGFUSE_PUBLIC_KEY": "lf-pk-test",
            "LANGFUSE_SECRET_KEY": "lf-sk-test",
            "LANGFUSE_HOST": "http://langfuse:3000",
        }
        with patch.dict("os.environ", env, clear=True):
            callbacks = _reload_and_call()

        assert len(callbacks) == 1
        assert type(callbacks[0]).__name__ == "LangchainCallbackHandler"

    def test_returns_empty_when_no_env_vars(self):
        with patch.dict("os.environ", _clean_env_no_langfuse(), clear=True):
            callbacks = _reload_and_call()

        assert callbacks == []

    def test_returns_empty_when_partial_env_vars(self):
        env = {
            **_clean_env_no_langfuse(),
            "LANGFUSE_PUBLIC_KEY": "lf-pk-test",
        }
        with patch.dict("os.environ", env, clear=True):
            callbacks = _reload_and_call()

        assert callbacks == []

    def test_returns_empty_when_keys_are_empty_strings(self):
        env = {
            **_clean_env_no_langfuse(),
            "LANGFUSE_PUBLIC_KEY": "",
            "LANGFUSE_SECRET_KEY": "",
            "LANGFUSE_HOST": "http://langfuse:3000",
        }
        with patch.dict("os.environ", env, clear=True):
            callbacks = _reload_and_call()

        assert callbacks == []


class TestBuildLangfuseMetadata:
    def test_minimal_metadata_agent_type_only(self):
        result = build_langfuse_metadata(agent_type="po")
        assert result["agent_type"] == "po"
        assert result["langfuse_tags"] == ["agent:po"]
        assert "langfuse_user_id" not in result
        assert "langfuse_session_id" not in result

    def test_full_metadata(self):
        result = build_langfuse_metadata(
            agent_type="architect",
            user_id="123",
            project_id="proj-abc",
            task_id="task-xyz",
            story_id="story-1",
        )
        assert result["langfuse_user_id"] == "123"
        assert result["langfuse_session_id"] == "proj-abc"
        assert result["task_id"] == "task-xyz"
        assert result["story_id"] == "story-1"
        assert result["agent_type"] == "architect"
        assert "agent:architect" in result["langfuse_tags"]
        assert "project:proj-abc" in result["langfuse_tags"]

    def test_skips_none_values(self):
        result = build_langfuse_metadata(agent_type="deploy", user_id="42")
        assert result["langfuse_user_id"] == "42"
        assert "langfuse_session_id" not in result
        assert "task_id" not in result
        assert "story_id" not in result
        assert result["langfuse_tags"] == ["agent:deploy"]

    def test_user_id_converted_to_string(self):
        result = build_langfuse_metadata(agent_type="po", user_id=12345)
        assert result["langfuse_user_id"] == "12345"

    def test_empty_user_id_not_included(self):
        result = build_langfuse_metadata(agent_type="po", user_id="")
        assert "langfuse_user_id" not in result
