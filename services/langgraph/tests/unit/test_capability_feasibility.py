"""Deterministic capability detection, with the user-selected workaround as its only waiver."""

import pytest

from shared.contracts.dto.product_brief import ProductBriefContent
from src.capability_feasibility import CAPABILITY_LIMITS, capability_conflicts


def _content(text, choices=()):
    return ProductBriefContent.model_validate(
        {
            "summary": "Product",
            "must_requirements": [{"id": "r1", "text": text}],
            "variant_choices": list(choices),
        }
    )


@pytest.mark.parametrize(
    ("text", "capability"),
    [
        ("OAuth web redirect", "oauth_web_redirect"),
        ("/connect: secure Google OAuth flow", "oauth_web_redirect"),
        ("/connect — secure Google OAuth flow", "oauth_web_redirect"),
        (
            "Хочу помощника Google Calendar с безопасной авторизацией через Google",
            "oauth_web_redirect",
        ),
        ("Sign in with Google", "oauth_web_redirect"),
        ("Set a redirect URI", "oauth_web_redirect"),
        ("Авторизация через Google", "oauth_web_redirect"),
        ("An inbound webhook", "inbound_webhooks"),
        ("Принимать входящие вебхуки", "inbound_webhooks"),
        ("A public HTTPS address", "https_domain"),
        ("Публичный HTTPS-адрес", "https_domain"),
        ("A custom domain", "custom_domain"),
        ("Собственный домен", "custom_domain"),
        ("Send email", "send_email"),
        ("Отправка писем", "send_email"),
        ("The bot sends email notifications to clients", "send_email"),
        ("Рассылка на почту клиентам", "send_email"),
        ("File storage", "file_storage"),
        ("Хранение файлов", "file_storage"),
    ],
)
def test_required_english_and_russian_phrases(text, capability):
    conflicts = capability_conflicts(_content(text))
    assert capability in {conflict.capability.id for conflict in conflicts}


@pytest.mark.parametrize("item", list(CAPABILITY_LIMITS.values()), ids=lambda item: item.id)
def test_all_manifest_terms_are_active(item):
    for term in item.detect:
        assert item.id in {
            conflict.capability.id for conflict in capability_conflicts(_content(term.upper()))
        }


@pytest.mark.parametrize(
    "text",
    [
        "A daily reminder using an in-process timer in the backend",
        "Бот присылает напоминание каждый день",
        "The user shares a calendar with a Google service account",
        "Use Telegram file ids and a database",
        "A backend available at http://<server IP>:<port>",
    ],
)
def test_supported_requirements_pass(text):
    assert capability_conflicts(_content(text)) == []


@pytest.mark.parametrize(
    "text",
    [
        "Save the client's name, phone and email",
        "адрес электронной почты клиента",
        "Google Sheets via service account, without OAuth",
        "/connect_notion with a pasted API token",
        "Бот хранит файлы по Telegram file id",
    ],
)
def test_supported_phrasings_near_a_missing_capability_do_not_trip(text):
    """A regression floor from the 1412 review, not a claim of completeness.

    Each phrasing only mentions a word the missing capability also uses; the detect
    terms name the capability itself, so none of these is refused.
    """
    assert capability_conflicts(_content(text)) == []


def test_acceptance_covers_only_the_named_capability():
    choice = {
        "feature": "Connect",
        "chosen": "Service account",
        "alternative": "OAuth",
        "trade_off": "Share calendar manually",
        "add_later": "Web sign-in when supported",
        "capability": "oauth_web_redirect",
    }
    text = "OAuth redirect and inbound webhooks"
    conflicts = capability_conflicts(_content(text, [choice]))
    assert [conflict.capability.id for conflict in conflicts] == ["inbound_webhooks"]
    del choice["capability"]
    assert len(capability_conflicts(_content(text, [choice]))) == 2


def test_original_user_wording_also_reaches_detection():
    content = _content("Connect the calendar")
    content.must_requirements[0].user_wording = "Хочу авторизацию через Google"
    assert capability_conflicts(content)[0].capability.id == "oauth_web_redirect"
