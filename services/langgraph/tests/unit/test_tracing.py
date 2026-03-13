"""Tests for Langfuse tracing utility."""

import importlib
import os
from unittest.mock import patch


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
