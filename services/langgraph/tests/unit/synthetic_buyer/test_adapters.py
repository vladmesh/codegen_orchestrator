"""The live adapters against controlled in-process transports: API, platform, Telegram, model."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import hashlib
import json
import os
from types import SimpleNamespace

import httpx
from langchain_core.messages import AIMessage
import pytest
import respx

from src.synthetic_buyer import platform_evidence, repository_evidence
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
from src.synthetic_buyer.platform_evidence import (
    LivePlatformFacts,
    ReaderUsageRefused,
    platform_product_id,
)
from src.synthetic_buyer.repository_evidence import (
    GitHubRepositoryFacts,
    RepositoryFactUnavailable,
    repository_name,
)
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


async def test_delete_and_absence_read_use_the_owner_authentication(api_routes):
    def handler(request):
        return httpx.Response(204 if request.method == "DELETE" else 404)

    api, seen = _api(api_routes, handler)

    assert await api.delete_project(PROJECT, 8202532144) == 204
    assert await api.deletion_confirmed(PROJECT, 8202532144)
    assert [r.method for r in seen] == ["DELETE", "GET"]
    assert all(r.url.path == f"/api/projects/{PROJECT}" for r in seen)
    assert all(r.headers["X-Telegram-ID"] == "8202532144" for r in seen)
    assert all(r.headers["X-Internal-Key"] == os.environ["INTERNAL_API_KEY"] for r in seen)


@pytest.mark.parametrize("status", [200, 202, 403, 409, 500])
async def test_delete_rejects_every_response_other_than_204(api_routes, status):
    api, _ = _api(api_routes, lambda request: httpx.Response(status, text=TOKEN))

    with pytest.raises(ApiRefused) as refused:
        await api.delete_project(PROJECT, 8202532144)

    assert refused.value.status == status
    assert TOKEN not in str(refused.value)


@pytest.mark.parametrize("status", [200, 403, 500])
async def test_only_owner_get_404_proves_deletion(api_routes, status):
    api, _ = _api(api_routes, lambda request: httpx.Response(status, text=TOKEN))
    if status == 200:
        assert not await api.deletion_confirmed(PROJECT, 8202532144)
    else:
        with pytest.raises(ApiRefused):
            await api.deletion_confirmed(PROJECT, 8202532144)


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


READER_KEY = "cps_abcdefghijk2_" + "B" * 43


def _reader(handler, redaction=None) -> tuple[LivePlatformFacts, Redaction]:
    async def stored(project_id):
        return {"PLATFORM_KEY": READER_KEY}

    redaction = redaction or Redaction()
    facts = LivePlatformFacts(
        CONFIG.platform,
        ENVIRON,
        redaction,
        stored_secrets=stored,
        http=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    return facts, redaction


async def test_reader_usage_is_read_from_the_released_nested_shape_with_the_products_key():
    seen = []

    def handler(request):
        seen.append(request)
        # The reader's released `Usage`: every counter lives in `used`.
        return httpx.Response(
            200,
            json={
                "product_id": "orch-example",
                "limits": {"channels_max": 50, "requests_per_minute": 60, "resolve_per_day": 200},
                "used": {"channels": 2, "requests_this_minute": 7, "resolves_today": 3},
            },
        )

    facts, redaction = _reader(handler)
    usage = await facts.usage(PROJECT, "https://reader.example.test/channels")

    assert (usage.product_id, usage.channels, usage.requests_this_minute, usage.resolves_today) == (
        "orch-example",
        2,
        7,
        3,
    )
    assert seen[0].url == "https://reader.example.test/channels/v1/usage"
    assert seen[0].headers["Authorization"] == "Bearer " + READER_KEY
    assert READER_KEY not in redaction.text(f"echo {READER_KEY}")


@pytest.mark.parametrize(
    "body",
    [
        {"product_id": "orch-example", "requests_this_minute": 7, "resolves_today": 3},
        {"product_id": "orch-example", "used": {"channels": 2, "requests_this_minute": "7"}},
        {"used": {"channels": 2, "requests_this_minute": 7, "resolves_today": 3}},
    ],
)
async def test_a_missing_or_malformed_usage_document_is_refused(body):
    facts, _ = _reader(lambda request: httpx.Response(200, json=body))

    with pytest.raises(ReaderUsageRefused):
        await facts.usage(PROJECT, "https://reader.example.test/channels")


async def test_a_refused_usage_read_keeps_its_status_not_its_body():
    facts, _ = _reader(lambda request: httpx.Response(401, json={"detail": READER_KEY}))

    with pytest.raises(ReaderUsageRefused, match="HTTP 401") as refused:
        await facts.usage(PROJECT, "https://reader.example.test/channels")
    assert READER_KEY not in str(refused.value)


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
    model = _Model("not json", '```json\n{"decision": "reply", "text": "Да."}\n```')

    turn = await ModelPersona(model, CONFIG.scenario).turn(PersonaContext(instruction="go"))

    assert (turn.decision, turn.text) == (PersonaDecision.REPLY, "Да.")
    assert len(model.calls) == 2


async def test_a_model_claiming_authority_it_does_not_have_is_an_invalid_turn():
    model = _Model('{"decision": "reply", "text": "Да.", "confirms_brief": true}', "{}")

    with pytest.raises(PersonaInvalid):
        await ModelPersona(model, CONFIG.scenario).turn(PersonaContext(instruction="go"))


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


class _App:
    """The GitHub App client's token surface, without a key."""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return None

    async def get_org_token(self, owner):
        return "installation-token-" + owner


def _github(monkeypatch, handler) -> tuple[GitHubRepositoryFacts, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def record(request):
        seen.append(request)
        return handler(request)

    monkeypatch.setattr(repository_evidence, "GitHubAppClient", _App)
    client = httpx.AsyncClient(transport=httpx.MockTransport(record))
    return GitHubRepositoryFacts(client=client), seen


async def test_repository_facts_are_read_only_and_typed(monkeypatch):
    def handler(request):
        path = request.url.path
        if path.endswith("/pulls/1"):
            return httpx.Response(
                200,
                json={
                    "number": 1,
                    "merged": True,
                    "head": {"sha": "3" * 40},
                    "base": {"sha": "1" * 40},
                    "merge_commit_sha": "4" * 40,
                },
            )
        if "/compare/" in path:
            return httpx.Response(
                200,
                json={
                    "status": "ahead",
                    "commits": [{"sha": "2" * 40}],
                    "files": [{"filename": "pyproject.toml"}],
                },
            )
        return httpx.Response(
            200,
            json={"id": 9, "head_sha": "4" * 40, "status": "completed", "conclusion": "success"},
        )

    facts, seen = _github(monkeypatch, handler)

    pull = await facts.pull_request("org/repo", 1)
    compare = await facts.compare("org/repo", "1" * 40, "2" * 40)
    run = await facts.workflow_run("org/repo", 9)

    assert (pull.merged, pull.base_sha, pull.merge_commit_sha) == (True, "1" * 40, "4" * 40)
    assert (compare.status, compare.commits, compare.files) == (
        "ahead",
        ("2" * 40,),
        ("pyproject.toml",),
    )
    assert (run.head_sha, run.conclusion) == ("4" * 40, "success")
    assert {request.method for request in seen} == {"GET"}
    assert seen[0].headers["Authorization"] == "token installation-token-org"
    assert seen[1].url.path == f"/repos/org/repo/compare/{'1' * 40}...{'2' * 40}"


async def test_an_unreadable_repository_fact_names_its_route_not_its_token(monkeypatch):
    facts, _ = _github(monkeypatch, lambda request: httpx.Response(404, json={"message": "x"}))

    with pytest.raises(RepositoryFactUnavailable, match="HTTP 404") as refused:
        await facts.pull_request("org/repo", 1)
    assert "installation-token" not in str(refused.value)


def test_only_a_github_repository_url_names_a_repository():
    assert repository_name("https://github.com/org/repo") == "org/repo"
    assert repository_name("https://github.com/org/repo.git") == "org/repo"
    assert repository_name("pending://slug") is None
    assert repository_name("https://example.test/org/repo") is None


async def test_a_channel_post_is_dated_by_the_channel_itself():
    class Client:
        async def get_messages(self, entity, ids):
            assert (entity, ids) == ("@chan_two", 201)
            return SimpleNamespace(date=datetime(2026, 10, 10, 12, 5, tzinfo=UTC))

    port = TelethonPort(CONFIG.telegram, ENVIRON, Redaction())
    port._client = Client()

    assert await port.post_date("chan_two", 201) == datetime(2026, 10, 10, 12, 5, tzinfo=UTC)


class _SetupClient:
    """Telethon's client as `TelethonPort.connect` builds it, under the test's control."""

    built: list = []
    authorization = None

    def __init__(self, session, api_id, api_hash, *, receive_updates):
        self.connected = False
        self.disconnect_hangs = False
        type(self).built.append(self)

    async def connect(self):
        self.connected = True

    async def is_user_authorized(self):
        if type(self).authorization is not None:
            return await type(self).authorization()
        return True

    async def disconnect(self):
        if self.disconnect_hangs:
            await asyncio.Event().wait()
        self.connected = False


@pytest.fixture
def setup_client(monkeypatch):
    import telethon

    _SetupClient.built = []
    _SetupClient.authorization = None
    monkeypatch.setattr(telethon, "TelegramClient", _SetupClient)
    monkeypatch.setattr("telethon.sessions.StringSession", lambda value: value)
    return _SetupClient


@pytest.mark.parametrize("failure", ["raises", "not_authorized"])
async def test_a_connection_whose_authorization_fails_is_kept_until_disconnected(
    setup_client, failure
):
    async def refuse():
        if failure == "raises":
            raise TimeoutError
        return False

    setup_client.authorization = staticmethod(refuse)
    port = TelethonPort(CONFIG.telegram, ENVIRON, Redaction())

    with pytest.raises(TransportError) as refused:
        await port.connect()

    (client,) = setup_client.built
    assert refused.value.stage == "authorization"
    assert client.connected and port.connected
    await port.disconnect()
    assert not client.connected and not port.connected


async def test_a_cancelled_disconnect_keeps_the_client_for_the_hold_to_account_for(setup_client):
    port = TelethonPort(CONFIG.telegram, ENVIRON, Redaction())
    await port.connect()
    (client,) = setup_client.built
    client.disconnect_hangs = True

    disconnecting = asyncio.create_task(port.disconnect())
    for _ in range(3):
        await asyncio.sleep(0)
    disconnecting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await disconnecting

    assert port.connected
    assert client.connected
