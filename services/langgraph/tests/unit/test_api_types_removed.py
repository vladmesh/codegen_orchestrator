from pathlib import Path


def test_dead_server_info_helper_is_removed():
    assert not (Path(__file__).parents[2] / "src/schemas/api_types.py").exists()
