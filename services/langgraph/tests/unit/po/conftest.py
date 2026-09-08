"""PO consumer unit-test fixtures shared across the package."""

import pytest

from src.consumers import po as po_consumer


@pytest.fixture(autouse=True)
def _fixed_po_summarization_config(monkeypatch):
    """Keep consumer-behaviour tests independent of the system-config API.

    These tests exercise Redis delivery/PEL semantics, not production config
    loading. Production startup coverage lives in test_agent_llm_env.py.
    """
    monkeypatch.setattr(
        po_consumer,
        "load_summarization_config",
        lambda _api_base_url: po_consumer.SummarizationConfig(
            max_tokens=1,
            trigger_tokens=1,
            max_summary_tokens=1,
        ),
    )
