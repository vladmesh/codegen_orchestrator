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
        / "service-template-91e582180b4295bce45155759bdad0dfa43b75f3"
        / "services/backend/src/controllers/settings.py"
    )

    assert f'detail="{CORE_SETTINGS_V1_UNDECLARED_KEY_DETAIL}"' in controller.read_text()
    assert f'detail="{CORE_SETTINGS_V1_VALUE_REJECTED_DETAIL}"' in controller.read_text()
