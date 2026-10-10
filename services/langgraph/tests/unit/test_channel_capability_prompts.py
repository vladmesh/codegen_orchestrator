"""Channel-content policy and generic PO defaults in the model-facing prompts."""

import pytest

from src.catalog_product_settings import render_po_capabilities
from src.prompts.platform_capabilities import (
    ARCHITECT_PLATFORM_CAPABILITIES_PROMPT,
    PLATFORM_CAPABILITIES_PROMPT,
)
from src.prompts.po import SYSTEM_PROMPT


def test_bot_brief_defaults_to_russian_and_english_with_one_product_language():
    prompt = " ".join(SYSTEM_PROMPT.split())
    assert (
        "By default, list Russian and English as the bot's languages in `must_requirements`"
        in prompt
    )
    assert '`key="language", scope="product", value="ru" or "en"`' in prompt
    assert "default to the user's language" in prompt
    assert "`language` is the brief's display language" in prompt


def test_explicit_single_language_overrides_the_bilingual_default():
    prompt = " ".join(SYSTEM_PROMPT.split())
    assert "An explicit single-language choice overrides the bilingual default" in prompt
    assert "use that language for the bot and its product setting" in prompt


def test_a_new_product_previews_a_ready_capability_before_its_first_brief(bundled_kit_catalog):
    """A capability-backed first brief: the preview comes before presenting, in one story."""
    prompt = " ".join(SYSTEM_PROMPT.split())
    block = " ".join(render_po_capabilities(bundled_kit_catalog).split())

    assert "plan two stories" not in prompt
    assert "Unless a capability preview asks the product language" in prompt
    assert (
        "call `preview_capabilities(project_id, requests)` after `create_project` and before "
        "`present_product_brief`" in block
    )
    assert "Product language is the user's explicit choice: never infer it" in block
    assert "Never mention packages, modules by name, versions or settings keys" in block


@pytest.mark.parametrize(
    "block",
    [PLATFORM_CAPABILITIES_PROMPT, ARCHITECT_PLATFORM_CAPABILITIES_PROMPT],
    ids=["po", "architect"],
)
def test_channel_content_uses_catalog_only_and_is_plannable_before_key_issuance(block):
    assert "public Telegram channels" in block
    assert "posts, digests and new-post delivery" in block
    assert "platform-backed catalog module" in block
    assert "Private channels and groups are not supported" in block
    assert "Products must not fetch or scrape t.me, Telegram web previews or Telegram APIs" in block
    assert "for channel content themselves" in block
    assert "Plan it now" in block
    assert "deployment" in block and "key issuance" in block


@pytest.mark.parametrize(
    "block",
    [PLATFORM_CAPABILITIES_PROMPT, ARCHITECT_PLATFORM_CAPABILITIES_PROMPT],
    ids=["po", "architect"],
)
def test_outbound_http_permission_explicitly_excepts_channel_content(block):
    outbound = next(
        line for line in block.splitlines() if line.startswith("- Connecting to other services")
    )
    assert "Exception: Telegram channel content" in outbound
    assert "only through a platform-backed catalog module" in outbound
