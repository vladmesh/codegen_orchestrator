"""Channel-content policy and generic PO defaults in the model-facing prompts."""

import pytest

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


def test_new_product_catalog_install_is_two_ordered_stories():
    prompt = " ".join(SYSTEM_PROMPT.split())
    assert "catalog module that cannot be installed into a draft product" in prompt
    assert "plan two stories" in prompt
    assert "first the base bot, then a second story to add the module" in prompt
    assert "Tell the user this sequence before the first brief" in prompt
    assert "confirm the module's own brief only after the base bot is ready" in prompt


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
