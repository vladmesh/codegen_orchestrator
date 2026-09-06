"""Pins the settings-core response discriminator to the released fixture."""

from scripts.template_pin import TEMPLATE_PIN
from shared.contracts.dto.settings_seed import (
    CORE_SETTINGS_V1_UNDECLARED_KEY_DETAIL,
    CORE_SETTINGS_V1_VALUE_REJECTED_DETAIL,
)


def test_core_v1_refusal_discriminators_match_the_pinned_template():
    controller = TEMPLATE_PIN.fixture_path() / "services/backend/src/controllers/settings.py"

    assert f'detail="{CORE_SETTINGS_V1_UNDECLARED_KEY_DETAIL}"' in controller.read_text()
    assert f'detail="{CORE_SETTINGS_V1_VALUE_REJECTED_DETAIL}"' in controller.read_text()
