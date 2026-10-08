"""Product setting conflicts use the kit's v1/v2 declarations."""

from pathlib import Path

from framework.bindings import load_binding
import pytest
import yaml

from src.install_probe import validate_binding_settings

DATA = Path(__file__).resolve().parents[3] / "langgraph/tests/unit/fixtures/catalog-install-v2"


@pytest.mark.parametrize("schema", [None, {"type": "string", "enum": ["ru", "en"]}])
def test_v2_without_timezone_admits_an_unowned_or_matching_language_setting(tmp_path, schema):
    binding = load_binding(DATA / "default.yaml")
    manifest = tmp_path / "services/tg_bot/manifest.yaml"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(
        yaml.safe_dump({"settings_schema": {"properties": {"interface_locale": schema}}})
    )
    assert validate_binding_settings(tmp_path, binding) is None


def test_v2_refuses_a_product_language_schema_conflict(tmp_path):
    binding = load_binding(DATA / "default.yaml")
    manifest = tmp_path / "services/tg_bot/manifest.yaml"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(
        yaml.safe_dump(
            {"settings_schema": {"properties": {"interface_locale": {"type": "integer"}}}}
        )
    )
    with pytest.raises(
        ValueError, match="binding_conflict: interface_locale schema is already owned"
    ):
        validate_binding_settings(tmp_path, binding)
