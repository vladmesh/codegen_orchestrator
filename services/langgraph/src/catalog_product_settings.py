"""Catalog snapshot settings shared by PO intake and Architect briefing."""

from typing import Literal

from framework.binding_product import binding_settings
from framework.bindings_v2 import BindingV2
from framework.spec.loader import _package_prefix
from framework.spec.packages import PackageManifest
from jsonschema import Draft202012Validator
from langchain_core.runnables import RunnableConfig
from pydantic import BaseModel, ConfigDict, Field
import yaml

from shared.contracts.dto.product_brief import ProductBriefContent, SettingScope

from .catalog_install import load_catalog_binding
from .kit_catalog import InstallablePackage, KitCatalog, KitCatalogAnswer, get_kit_catalog_reader
from .prompts.po import CATALOG_SETTINGS_PROMPT

PO_CATALOG_CONFIG_KEY = "po_kit_catalog"
PO_PACKAGES_CONFIG_KEY = "po_package_settings"

# The reader reuses a frozen snapshot until refresh. Retain only its latest
# rendering; an unavailable answer never substitutes this block for fresh data.
_rendered_packages: tuple[KitCatalog, str] | None = None


class ProductSetting(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    key: str
    schema_data: dict = Field(alias="schema")
    required: bool
    description: str
    seed: bool = False
    purpose: str


class SettingQuestion(ProductSetting):
    package: str
    issue: Literal["missing", "invalid_value"]


class PackageSettingsRefusal(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["package_settings_required"] = "package_settings_required"
    settings: list[SettingQuestion]
    instruction: str


class PackageCatalogRefusal(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal[
        "unknown_catalog_packages", "package_settings_unavailable", "package_catalog_unavailable"
    ]
    packages: list[str]
    instruction: str


async def turn_catalog(config: RunnableConfig) -> KitCatalogAnswer:
    """A tool sees the same snapshot as this turn's prompt."""
    configurable = config["configurable"]
    if PO_CATALOG_CONFIG_KEY in configurable:
        return configurable[PO_CATALOG_CONFIG_KEY]
    return await get_kit_catalog_reader().read()


async def po_catalog_context() -> dict:
    """Read and render once per turn, including turns with many model steps."""
    catalog = await get_kit_catalog_reader().read()
    return {
        PO_CATALOG_CONFIG_KEY: catalog,
        PO_PACKAGES_CONFIG_KEY: render_po_packages(catalog),
    }


def catalog_binding_settings(catalog: KitCatalog, name: str) -> dict[str, dict]:
    """The pinned kit owns binding version dispatch and setting schemas."""
    return binding_settings(load_catalog_binding(catalog.bindings[name]))


def package_product_settings(catalog: KitCatalog, item: InstallablePackage) -> list[ProductSetting]:
    package = item.package
    settings = []
    if package.name in catalog.bindings:
        binding = load_catalog_binding(catalog.bindings[package.name])
        for key, schema in catalog_binding_settings(catalog, package.name).items():
            purpose = (
                "language"
                if isinstance(binding, BindingV2) and binding.language.key == key
                else "timezone"
            )
            settings.append(
                ProductSetting(
                    key=key,
                    schema=schema,
                    required="default" not in schema,
                    description=(
                        "product language: one of " + ", ".join(schema["enum"])
                        if purpose == "language"
                        else "product timezone: " + schema["format"]
                    ),
                    purpose=purpose,
                )
            )
    return settings + package_owned_settings(catalog, item)


def package_owned_settings(catalog: KitCatalog, item: InstallablePackage) -> list[ProductSetting]:
    """Only namespaced manifest settings identify package ownership."""
    package = item.package
    descriptions = {setting.name: setting.summary for setting in package.settings}
    settings = []
    if package.name in catalog.manifests:
        manifest = PackageManifest.model_validate(yaml.safe_load(catalog.manifests[package.name]))
        seeds = {seed.key for seed in manifest.setting_seeds}
        for local_key, schema in manifest.settings_schema["properties"].items():
            settings.append(
                ProductSetting(
                    key=f"{_package_prefix(package.name)}.{local_key}",
                    schema=schema,
                    required=local_key not in seeds and "default" not in schema,
                    description=descriptions.get(
                        local_key, schema.get("description", package.summary)
                    ),
                    seed=local_key in seeds,
                    purpose="package setting",
                )
            )
    return settings


def render_po_packages(catalog: KitCatalogAnswer) -> str:
    """All installable catalog capabilities, with compact product-setting data.

    The manifest admits catalog installation generically. The live catalog owns
    the individual capabilities; no static package list belongs in the manifest.
    """
    if not isinstance(catalog, KitCatalog) or not catalog.packages:
        return ""
    global _rendered_packages
    if _rendered_packages is not None and _rendered_packages[0] is catalog:
        return _rendered_packages[1]
    lines = [CATALOG_SETTINGS_PROMPT.rstrip()]
    for item in catalog.packages:
        package = item.package
        lines.append(f"- {package.name}: {package.summary}")
        lines.append("  capabilities: " + "; ".join(package.capabilities))
        try:
            settings = package_product_settings(catalog, item)
        except ValueError as error:
            lines.append(f"  settings unavailable: {error}")
        else:
            lines.extend("  " + setting.model_dump_json(by_alias=True) for setting in settings)
    block = "\n".join(lines)
    _rendered_packages = (catalog, block)
    return block


def package_settings_refusal(
    content: ProductBriefContent,
    catalog: KitCatalogAnswer,
    brief_id: str,
    catalog_packages: list[str],
) -> PackageSettingsRefusal | PackageCatalogRefusal | None:
    """Check declared packages and owned keys; generic binding keys identify none."""
    if not isinstance(catalog, KitCatalog):
        if catalog_packages:
            return PackageCatalogRefusal(
                status="package_catalog_unavailable",
                packages=catalog_packages,
                instruction=(
                    "The catalog is unavailable. Retry when it is readable; "
                    "never guess package settings."
                ),
            )
        return None
    declared = set(catalog_packages)
    if unknown := sorted(declared - catalog.names):
        return PackageCatalogRefusal(
            status="unknown_catalog_packages",
            packages=unknown,
            instruction=(
                "Nothing was confirmed. Declare exact names from this turn's catalog, "
                "or [] for an ordinary brief."
            ),
        )
    product_values = {
        setting.key: setting.value
        for setting in content.initial_settings
        if setting.scope == SettingScope.PRODUCT
    }
    all_keys = {setting.key for setting in content.initial_settings}
    questions = []
    for item in catalog.packages:
        relevant = item.name in declared
        try:
            owned = package_owned_settings(catalog, item)
            relevant = relevant or any(setting.key in all_keys for setting in owned)
            settings = package_product_settings(catalog, item) if relevant else []
        except ValueError:
            if relevant:
                return PackageCatalogRefusal(
                    status="package_settings_unavailable",
                    packages=[item.name],
                    instruction=(
                        "Nothing was confirmed. The kit refuses this package's binding "
                        "or manifest; repair catalog data before retrying."
                    ),
                )
            continue
        if not relevant:
            continue
        for setting in settings:
            issue = None
            if setting.required and setting.key not in product_values:
                issue = "missing"
            elif setting.key in product_values and not Draft202012Validator(
                setting.schema_data
            ).is_valid(product_values[setting.key]):
                issue = "invalid_value"
            if issue:
                questions.append(
                    SettingQuestion(
                        **setting.model_dump(by_alias=True), package=item.name, issue=issue
                    )
                )
    if not questions:
        return None
    return PackageSettingsRefusal(
        settings=questions,
        instruction=(
            "Nothing was confirmed. Ask the user for each listed required value, showing "
            "the allowed choices in their language. Never infer product language from "
            "conversation language. Put named items into the exact seeded package key; "
            "do not invent a key. Present a new revision with "
            f"corrects_brief_id='{brief_id}', using these product-scope keys and descriptions "
            "in the user's language (at most 6 settings, descriptions at most 150 characters), "
            "then wait for confirmation."
        ),
    )
