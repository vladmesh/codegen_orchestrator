"""The live adapters against controlled in-process transports: API, platform, Telegram, model."""

from __future__ import annotations

from datetime import UTC, datetime
import hashlib
import json
import os
from types import SimpleNamespace

import httpx
from langchain_core.messages import AIMessage
import pytest
import respx

from src.synthetic_buyer import platform_evidence
from src.synthetic_buyer.codegen_api import ApiRefused, CodegenApi
from src.synthetic_buyer.config import parse_config
from src.synthetic_buyer.evidence import Redaction
from src.synthetic_buyer.persona import (
    ModelPersona,
    PersonaContext,
    PersonaDecision,
    PersonaInvalid,
    PersonaTurn,
    deviation,
)
from src.synthetic_buyer.platform_evidence import LivePlatformFacts, platform_product_id
from src.synthetic_buyer.telegram import TelethonPort, TransportError, to_message
from tests.unit.synthetic_buyer.fakes import ENVIRON, PROJECT, PROMO, TOKEN, config_data

CONFIG = parse_config(config_data())


@pytest.fixture
def api_routes():
    with respx.mock(base_url="http://api:8000", assert_all_called=False) as router:
        yield router


def _api(router, handler) -> tuple[CodegenApi, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    router.route().mock(side_effect=record)
    return CodegenApi("http://api:8000"), seen


async def test_the_rollout_write_keeps_every_unrelated_key_and_id(api_routes):
    def handler(request):
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "value": {"project_ids": ["a" * 8 + "-1111-4111-8111-" + "1" * 12], "note": 1}
                },
            )
        return httpx.Response(200, json={"value": json.loads(request.content)["value"]})

    api, seen = _api(api_routes, handler)
    before = await api.module_rollout()
    await api.write_module_rollout({**before, "project_ids": [*before["project_ids"], PROJECT]})

    patch = seen[-1]
    assert (patch.method, patch.url.path) == (
        "PATCH",
        "/api/system-configs/capabilities.module_rollout",
    )
    assert json.loads(patch.content)["value"] == {
        "project_ids": ["a" * 8 + "-1111-4111-8111-" + "1" * 12, PROJECT],
        "note": 1,
    }
    assert patch.headers["X-Internal-Key"] == os.environ["INTERNAL_API_KEY"]


async def test_an_owned_read_asks_as_the_buyer(api_routes):
    api, seen = _api(api_routes, lambda request: httpx.Response(200, json=[]))

    await api.owned_projects(8202532144)

    assert seen[0].headers["X-Telegram-ID"] == "8202532144"
    assert seen[0].url.path == "/api/projects/"


async def test_a_refusal_keeps_its_route_and_status_but_never_the_body(api_routes):
    api, _ = _api(
        api_routes, lambda request: httpx.Response(409, json={"detail": f"code {PROMO} taken"})
    )

    with pytest.raises(ApiRefused) as refused:
        await api.mint_promo(credits_microusd=1, reservation_microusd=1)

    assert (refused.value.route, refused.value.status) == ("/api/promo-codes/batch", 409)
    assert PROMO not in str(refused.value) + refused.value.detail


async def test_a_mint_that_is_not_one_code_is_refused(api_routes):
    api, _ = _api(
        api_routes, lambda request: httpx.Response(201, json=[{"code": "A"}, {"code": "B"}])
    )

    with pytest.raises(ApiRefused):
        await api.mint_promo(credits_microusd=1, reservation_microusd=1)


def test_the_platform_product_id_is_the_deploy_resolvers():
    expected = "orch-" + hashlib.sha256(PROJECT.encode()).hexdigest()[:58]

    assert platform_product_id(PROJECT) == expected
    assert len(expected) == 63


async def test_reader_usage_is_read_with_the_products_own_key_and_redacted():
    key = "cps_abcdefghijk2_" + "B" * 43
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(
            200, json={"used": {"channels": 2}, "requests_this_minute": 0, "resolves_today": 3}
        )

    async def stored(project_id):
        return {"PLATFORM_KEY": key}

    redaction = Redaction()
    facts = LivePlatformFacts(
        CONFIG.platform,
        ENVIRON,
        redaction,
        stored_secrets=stored,
        http=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )

    usage = await facts.usage(PROJECT, "https://reader.example.test/tg-reader")

    assert (usage.status, usage.channels_used, usage.resolves_today) == (200, 2, 3)
    assert seen[0].url == "https://reader.example.test/tg-reader/v1/usage"
    assert seen[0].headers["Authorization"] == "Bearer " + key
    assert key not in redaction.text(f"echo {key}")


async def test_auth_facts_match_the_stored_key_against_registered_keys(monkeypatch):
    key = "cps_abcdefghijk2_" + "C" * 43
    asked = []

    class AdminClient:
        def __init__(self, url, token):
            asked.append((url, token.get_secret_value()))

        async def product_keys(self, product_id):
            asked.append(product_id)
            return [
                SimpleNamespace(key_id="abcdefghijk2", revoked_at=None),
                SimpleNamespace(key_id="zzzzzzzzzzzz", revoked_at=datetime(2026, 1, 1, tzinfo=UTC)),
            ]

        async def aclose(self):
            pass

    async def stored(project_id):
        return {"PLATFORM_KEY": key}

    monkeypatch.setattr(platform_evidence, "PlatformAuthAdminClient", AdminClient)
    facts = await LivePlatformFacts(
        CONFIG.platform, ENVIRON, Redaction(), stored_secrets=stored
    ).auth(PROJECT)

    assert asked[0] == ("http://auth:8000", "admin-token-value-abcdef")
    assert asked[1] == platform_product_id(PROJECT)
    assert facts.stored_key_active
    assert facts.revoked_key_ids == ("zzzzzzzzzzzz",)


async def test_a_language_switch_needs_the_stored_settings_capability():
    async def stored(project_id):
        return {}

    with pytest.raises(RuntimeError, match="settings write capability"):
        await LivePlatformFacts(
            CONFIG.platform, ENVIRON, Redaction(), stored_secrets=stored
        ).switch_language(PROJECT, "https://product.example.test", "en")


def test_a_telethon_message_is_reduced_to_what_the_controller_reads():
    button = SimpleNamespace(text="Удалить", data=b"rm:1", url=None)
    item = SimpleNamespace(
        id=42,
        sender_id=7002,
        out=False,
        date=datetime(2026, 10, 10, tzinfo=UTC),
        raw_text="@chan_two\nНовый пост\nhttps://t.me/chan_two/201",
        reply_markup=SimpleNamespace(rows=[SimpleNamespace(buttons=[button])]),
        entities=[SimpleNamespace(url="https://t.me/chan_two/202")],
        fwd_from=SimpleNamespace(from_id=SimpleNamespace(channel_id=1001)),
        reply_to=None,
        edit_date=None,
    )

    message = to_message(item)

    assert message.urls == ("https://t.me/chan_two/201", "https://t.me/chan_two/202")
    assert message.buttons[0].data == b"rm:1"
    assert message.forwarded_from_channel == 1001


async def test_a_telethon_failure_keeps_its_stage_and_class_but_not_its_text():
    class Client:
        async def get_me(self):
            raise ValueError(f"session {TOKEN} broke")

    port = TelethonPort(CONFIG.telegram, ENVIRON, Redaction())
    port._client = Client()

    with pytest.raises(TransportError) as failed:
        await port.me()

    assert (failed.value.stage, failed.value.detail) == ("get_me", "failed: ValueError")
    assert TOKEN not in str(failed.value)


async def test_an_unset_session_is_refused_before_telegram_is_asked():
    port = TelethonPort(CONFIG.telegram, {**ENVIRON, "TELETHON_SESSION": ""}, Redaction())

    with pytest.raises(Exception, match="env:TELETHON_SESSION"):
        await port.connect()
    assert not port.connected


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ("Хочу посты из каналов @chan_one и @chan_two.", None),
        ("Нужны ключевые новости, без кодировки.", None),
        ("Пришлите код.", "implementation_instruction"),
        ("Поставьте модуль для каналов.", "implementation_instruction"),
        ("Нужна последняя версия.", "implementation_instruction"),
        ("Вот ключ от сервиса.", "implementation_instruction"),
        ("Используйте API каналов.", "implementation_instruction"),
        (f"Токен {TOKEN}", "credential_shaped_text"),
        ("I want channel posts please.", "not_russian"),
        ("Добавьте ещё @other_channel.", "unknown_channel"),
        ("", "empty_reply"),
    ],
)
def test_the_customer_never_leaves_the_scenario(text, reason):
    turn = PersonaTurn(decision=PersonaDecision.REPLY, text=text)

    assert deviation(turn, CONFIG.scenario) == reason


class _Model:
    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = []

    async def ainvoke(self, messages):
        self.calls.append(messages)
        return AIMessage(self.answers.pop(0))


async def test_one_invalid_model_answer_is_reasked_once():
    model = _Model("not json", '```json\n{"decision": "wait", "bot_asks_for_token": true}\n```')

    turn = await ModelPersona(model, CONFIG.scenario).turn(PersonaContext(instruction="go"))

    assert (turn.decision, turn.bot_asks_for_token) == (PersonaDecision.WAIT, True)
    assert len(model.calls) == 2


async def test_two_invalid_model_answers_are_a_refusal():
    model = _Model("{}", '{"decision": "sing"}')

    with pytest.raises(PersonaInvalid):
        await ModelPersona(model, CONFIG.scenario).turn(PersonaContext(instruction="go"))


async def test_the_persona_prompt_names_the_scenario_and_no_secret():
    model = _Model('{"decision": "wait"}')

    await ModelPersona(model, CONFIG.scenario).turn(
        PersonaContext(instruction="go", latest=[{"text": "Привет", "buttons": []}])
    )

    system, human = model.calls[0]
    assert "@chan_one" in system.content and "@chan_two" in system.content
    for secret in (TOKEN, PROMO, ENVIRON["TELETHON_SESSION"]):
        assert secret not in system.content + human.content
