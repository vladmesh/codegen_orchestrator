"""Deterministic capability detection, with the user-selected workaround as its only waiver."""

import pytest

from shared.contracts.dto.product_brief import ProductBriefContent
from src.capability_feasibility import CAPABILITY_LIMITS, capability_conflicts, capability_refusal


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
        ("A public HTTPS address", "web_presence"),
        ("Публичный HTTPS-адрес", "web_presence"),
        ("A custom domain", "web_presence"),
        ("Собственный домен", "web_presence"),
        ("Сделать сайт", "web_presence"),
        ("Админка для владельца", "web_presence"),
        ("Личный кабинет клиента", "web_presence"),
        ("An admin panel for the owner", "web_presence"),
        ("A landing page for the bot", "web_presence"),
        ("Принимать оплату через ЮKassa", "payments"),
        ("Принимать оплату через ЮКассу", "payments"),
        ("Accept payments with Stripe", "payments"),
        ("Оплата подписки в Telegram Stars", "payments"),
        ("Ежедневный бэкап данных", "backups"),
        ("Резервная копия базы каждую ночь", "backups"),
        ("Nightly backup of the data", "backups"),
        ("Хранить фото на сервере", "file_storage"),
        ("Загрузка файлов пользователями", "file_storage"),
        ("Users upload files to the bot", "file_storage"),
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


@pytest.mark.parametrize("item", list(CAPABILITY_LIMITS.values()), ids=lambda item: item.id)
def test_all_weak_terms_are_active_on_their_own(item):
    for term in item.detect_weak:
        assert item.id in {
            conflict.capability.id for conflict in capability_conflicts(_content(term.upper()))
        }


@pytest.mark.parametrize(
    ("text", "capability"),
    [
        ("payment", "payments"),
        ("оплата", "payments"),
        ("платёж", "payments"),
        ("платеж", "payments"),
        ("upload", "file_storage"),
        ("Оплата подписки в боте", "payments"),
        ("Users upload their documents", "file_storage"),
    ],
)
def test_the_bare_words_the_owner_listed_trip(text, capability):
    assert capability in {
        conflict.capability.id for conflict in capability_conflicts(_content(text))
    }


@pytest.mark.parametrize(
    ("text", "capability"),
    [
        ("Record the payment through YooKassa", "payments"),
        ("Учёт расходов и приём оплаты через ЮKassa", "payments"),
        ("Upload files and keep their Telegram file id", "file_storage"),
    ],
)
def test_a_recording_cue_does_not_excuse_a_strong_term(text, capability):
    assert capability in {
        conflict.capability.id for conflict in capability_conflicts(_content(text))
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
        "Use the OAuth flow supported by a device-code client",
        "device code flow (OAuth device authorization)",
    ],
)
def test_supported_phrasings_near_a_missing_capability_do_not_trip(text):
    """A regression floor from the 1412 review, not a claim of completeness.

    Each phrasing only mentions a word the missing capability also uses; the detect
    terms name the capability itself, so none of these is refused.
    """
    assert capability_conflicts(_content(text)) == []


@pytest.mark.parametrize(
    "text",
    [
        "Бот-трекер привычек: пользователь отмечает привычку командой /done и кнопками в меню",
        "Бот хранит историю отметок и показывает статистику за неделю",
        "Каждый вечер в 21:00 бот присылает напоминание отметить привычки",
        "Владелец меняет список привычек по умолчанию без новой версии бота",
        "Выгрузка истории командой /export в CSV",
        "Бот учитывает расходы: записывает платежи за аренду и оплату связи",
        "A habit tracker bot: /done marks a habit, the bot keeps history and sends reminders",
        "The bot records rent payments and shows a monthly report",
        "The user sends a photo of a receipt and the bot keeps its Telegram file id",
        "Учёт платежей по кредиту: бот отмечает, какой платёж уже внесён",
        "Track which payment is due next and remind about it",
        "Бот записывает оплату коммуналки и показывает расходы за месяц",
        "The user can upload a receipt photo; the bot keeps its file id",
    ],
)
def test_an_ordinary_tracker_bot_brief_trips_nothing(text):
    """False-positive floor: a tracker bot without the missing capabilities is not refused."""
    assert capability_conflicts(_content(text)) == []


def test_the_refusal_reason_is_built_from_product_fields_only():
    [conflict] = capability_conflicts(_content("Принимать оплату через ЮKassa"))
    item = conflict.capability

    assert item.id == "payments"
    assert item.plain in conflict.reason and item.why in conflict.reason
    assert not hasattr(item, "technical")
    assert "webhook" not in conflict.reason and "https" not in conflict.reason


@pytest.mark.parametrize(
    "old_id",
    ["https_domain", "custom_domain", "public_base_url", "telegram_mini_app", "web_frontend"],
)
def test_a_choice_recorded_under_a_merged_v4_id_still_waives_it(old_id):
    choice = {
        "feature": "Links",
        "chosen": "Chat buttons",
        "alternative": "Own site",
        "trade_off": "No web page",
        "add_later": "A site when supported",
        "capability": old_id,
    }

    assert capability_conflicts(_content("Собственный домен", [choice])) == []


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


@pytest.mark.parametrize(
    ("text", "capability", "name"),
    [
        ("Принимать оплату через ЮKassa", "payments", "Accepting payments"),
        ("Сделать сайт для бота", "web_presence", "A website or web pages"),
    ],
)
def test_the_po_refusal_names_the_limitation(text, capability, name):
    refusal = capability_refusal(_content(text))

    assert refusal is not None
    assert f"{capability} ({name})" in refusal and "not possible now" in refusal
