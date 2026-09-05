"""Pins the settings-core response discriminator to the released fixture."""

from pathlib import Path

from shared.contracts.dto.settings_seed import (
    CORE_SETTINGS_V1_UNDECLARED_KEY_DETAIL,
    CORE_SETTINGS_V1_VALUE_REJECTED_DETAIL,
)


def test_core_v1_refusal_discriminators_match_the_pinned_template():
    controller = (
        Path(__file__).parents[1]
        / "fixtures"
        / "service-template-40b54d87dbfe64a9fa6ec379820e43137aaba04c"
        / "services/backend/src/controllers/settings.py"
    )

    assert f'detail="{CORE_SETTINGS_V1_UNDECLARED_KEY_DETAIL}"' in controller.read_text()
    assert f'detail="{CORE_SETTINGS_V1_VALUE_REJECTED_DETAIL}"' in controller.read_text()
