from pydantic import ValidationError
import pytest

from shared.contracts.dto.product_brief import ProductBriefContent


def test_bilingual_brief_keeps_settings_as_typed_json() -> None:
    source = (
        "\u00ab\u041d\u0443\u0436\u0435\u043d \u0431\u043e\u0442"
        + " \u043d\u0430 \u0434\u0432\u0443\u0445 \u044f\u0437\u044b\u043a\u0430\u0445\u00bb"
    )
    brief = ProductBriefContent.model_validate(
        {
            "intended_users": ["Russian and English speaking founders"],
            "languages": ["ru", "en"],
            "must_requirements": [
                {
                    "id": "must-greet",
                    "text": "Greet users",
                    "source": source,
                }
            ],
            "initial_settings": [
                {"key": "settings.languages", "value": ["ru", "en"], "scope": "product"}
            ],
        }
    )

    assert brief.initial_settings[0].value == ["ru", "en"]


@pytest.mark.parametrize(
    "payload",
    [
        {"must_requirements": [{"id": "", "text": "x", "source": "x"}]},
        {
            "must_requirements": [
                {"id": "same", "text": "x", "source": "x"},
                {"id": "same", "text": "y", "source": "y"},
            ]
        },
        {"initial_settings": [{"key": "api_key", "value": "sk-live", "scope": "product"}]},
    ],
)
def test_product_brief_rejects_invalid_or_secret_bearing_content(payload: dict) -> None:
    with pytest.raises(ValidationError):
        ProductBriefContent.model_validate(payload)
