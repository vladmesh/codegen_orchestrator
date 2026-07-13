import importlib


def test_dead_server_info_helper_is_removed():
    api_types = importlib.import_module("src.schemas.api_types")

    assert not hasattr(api_types, "ServerInfo")
    assert not hasattr(api_types, "get_server_ip")
