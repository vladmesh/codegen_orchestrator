"""A fictional v2 package with a product-scope seed, from the 1564 fixture."""

from dataclasses import replace
from pathlib import Path

from framework.catalog import CatalogSetting, bundled_catalog, parse_catalog
import yaml

from src.kit_catalog import installable


def notebook_snapshot():
    data = Path(__file__).parents[1] / "fixtures/catalog-install-v2"
    raw = (data / "catalog.yaml").read_text()
    catalog = installable(parse_catalog(raw), "fixture")
    item = catalog.packages[0]
    package = replace(
        item.package,
        settings=(CatalogSetting("starting_notes", "Named notes copied once per user."),),
    )
    manifest = yaml.safe_load((data / "package.yaml").read_text())
    manifest["settings_schema"] = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "properties": {"starting_notes": {"type": "array", "items": {"type": "string"}}},
        "additionalProperties": False,
    }
    manifest["setting_seeds"] = [{"key": "starting_notes", "scope": "product"}]
    return replace(
        catalog,
        packages=(replace(item, package=package),),
        bindings={"notebook": (data / "default.yaml").read_text()},
        manifests={"notebook": yaml.safe_dump(manifest)},
        raw=raw,
    )


def platform_and_v1_snapshot():
    """Released v2 binding plus the v1 fixture, preserving generic setting keys."""
    data = Path(__file__).parents[1] / "fixtures"
    v2_binding = (data / "platform-module/binding.yaml").read_text()
    v1_binding = (data / "catalog-install/default.yaml").read_text()
    v2_name = yaml.safe_load(v2_binding)["package"]
    v1_name = yaml.safe_load(v1_binding)["package"]
    catalog = installable(bundled_catalog(), "fixture")
    selected = tuple(item for item in catalog.packages if item.name in {v2_name, v1_name})
    v2 = next(item for item in selected if item.name == v2_name)
    seed_key = v2.package.settings[0].name
    # Settings-only package projection: identity and setting name come from the
    # bundled catalog; no runtime actions are needed for PO setting admission.
    manifest = {
        "protocol_version": 1,
        "name": v2.name,
        "version": v2.version.version,
        "requires_core": v2.version.requires_core,
        "http": {"prefix": "/channels"},
        "settings_schema": {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
            "properties": {seed_key: {"type": "array", "items": {"type": "string"}}},
            "additionalProperties": False,
        },
        "setting_seeds": [{"key": seed_key, "scope": "product"}],
    }
    return replace(
        catalog,
        packages=selected,
        bindings={v2_name: v2_binding, v1_name: v1_binding},
        manifests={
            v2_name: yaml.safe_dump(manifest),
            v1_name: (data / "catalog-install/package.yaml").read_text(),
        },
    ), v2_name
