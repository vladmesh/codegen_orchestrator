"""Boundary tests for the PO tools aggregate module."""

from src.agents.po import tools
from src.agents.po.tools_shared import init_po_clients

_PRIVATE_SHARED_HELPERS = ("_get_api", "_get_stream_client", "_user_headers")


def test_tools_facade_does_not_export_shared_internals() -> None:
    for name in _PRIVATE_SHARED_HELPERS:
        assert not hasattr(tools, name)
        assert name not in tools.__all__


def test_startup_initializer_remains_explicitly_exported() -> None:
    assert tools.init_po_clients is init_po_clients
    assert "init_po_clients" in tools.__all__
