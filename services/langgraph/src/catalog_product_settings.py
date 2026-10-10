"""The activated catalog as the PO sees it: user-level capabilities, never packages.

One read per PO turn serves the system prompt and every tool of that turn. The PO is
shown `capability_offers` — opaque capability ids with their user-level summary and
phrases — and nothing else from the catalog: no package or library name, version,
binding, install recipe or settings key. What a capability means for a project is the
Architect's preview (`capability_preview`), reached through the PO's
`preview_capabilities` tool. The Architect's briefing keeps its technical view through
`catalog_binding_settings`.
"""

from langchain_core.runnables import RunnableConfig

from .capability_preview import capability_offers
from .catalog_install import load_catalog_binding
from .kit_catalog import KitCatalog, KitCatalogAnswer, get_kit_catalog_reader
from .prompts.po import CAPABILITY_OFFERS_PROMPT, CAPABILITY_OFFERS_UNAVAILABLE_PROMPT

PO_CATALOG_CONFIG_KEY = "po_kit_catalog"
PO_CAPABILITIES_CONFIG_KEY = "po_capability_offers"

# The reader reuses a frozen snapshot until refresh. Retain only its latest
# rendering; an unavailable answer never substitutes this block for fresh data.
_rendered_offers: tuple[KitCatalog, str] | None = None


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
        PO_CAPABILITIES_CONFIG_KEY: render_po_capabilities(catalog),
    }


def catalog_binding_settings(catalog: KitCatalog, name: str) -> dict[str, dict]:
    """The pinned kit owns binding version dispatch and setting schemas."""
    from framework.binding_product import binding_settings

    return binding_settings(load_catalog_binding(catalog.bindings[name]))


def render_po_capabilities(catalog: KitCatalogAnswer) -> str:
    """The capabilities the activated catalog offers as ready modules, product terms only."""
    if not isinstance(catalog, KitCatalog):
        return CAPABILITY_OFFERS_UNAVAILABLE_PROMPT.rstrip()
    global _rendered_offers
    if _rendered_offers is not None and _rendered_offers[0] is catalog:
        return _rendered_offers[1]
    offers = capability_offers(catalog)
    if not offers:
        return ""
    lines = [CAPABILITY_OFFERS_PROMPT.rstrip()]
    for offer in offers:
        lines.append(f"- {offer.capability_id}: {offer.summary}")
        lines.append("  for example: " + "; ".join(offer.phrases))
    block = "\n".join(lines)
    _rendered_offers = (catalog, block)
    return block
