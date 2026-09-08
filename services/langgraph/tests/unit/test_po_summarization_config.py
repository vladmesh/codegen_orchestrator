"""PO summarization tuning is required system config, not env fallback."""

from unittest.mock import MagicMock

import pytest

from shared.config_store import ConfigStoreUnavailableError
from src.consumers import po


def test_loads_all_effective_values_from_system_config(monkeypatch):
    store = MagicMock()
    store.get_int.side_effect = [50_000, 60_000, 2_000]
    monkeypatch.setattr(po, "ConfigStore", lambda _api_base_url: store)

    config = po.load_summarization_config("http://api:8000")

    store.validate_required.assert_called_once_with(list(po.SUMMARIZATION_CONFIG_KEYS))
    assert config == po.SummarizationConfig(
        max_tokens=50_000,
        trigger_tokens=60_000,
        max_summary_tokens=2_000,
    )
    assert [call.args[0] for call in store.get_int.call_args_list] == list(
        po.SUMMARIZATION_CONFIG_KEYS
    )


def test_unavailable_system_config_propagates_without_fallback(monkeypatch):
    store = MagicMock()
    store.validate_required.side_effect = ConfigStoreUnavailableError("system config unavailable")
    monkeypatch.setattr(po, "ConfigStore", lambda _api_base_url: store)

    with pytest.raises(ConfigStoreUnavailableError, match="system config unavailable"):
        po.load_summarization_config("http://api:8000")

    store.get_int.assert_not_called()
