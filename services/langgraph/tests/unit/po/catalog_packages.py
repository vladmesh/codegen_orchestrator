"""A fictional v2 package with a product-scope seed, from the 1564 fixture."""

from dataclasses import replace
from pathlib import Path

from framework.catalog import CatalogSetting, parse_catalog
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
