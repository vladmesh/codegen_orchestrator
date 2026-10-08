"""Stand response data satisfies the released API projection and binding conversation."""

from datetime import UTC, datetime
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
from jsonschema import Draft202012Validator, FormatChecker
import yaml

from shared.stand_fake_platform import create_app
from src.consumers import stand_conversation
from tests.unit.test_stand_conversation import Transport

DATA = Path(__file__).parent / "fixtures/platform-module"
INFRA = Path(__file__).resolve().parents[4] / "infra"


async def test_service_fixture_authenticates_and_returns_fresh_contract_data():
    fixture = json.loads((INFRA / "stand-platform-fixtures/platform-module.json").read_text())
    api = json.loads((DATA / "openapi.json").read_text())
    app = create_app("private-admin", fixture)
    key = "cps_abcdefghijkl_" + "x" * 43
    headers = {"Authorization": "Bearer private-admin", "If-None-Match": "*"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://fake"
    ) as http:
        base = "/admin/v1/products/product"
        for path, body in [
            (base, {"display_name": "Stand", "orchestrator_project_id": "project"}),
            (base + "/grants/catalog-service", {"scopes": ["read"], "quota": {"items": 50}}),
            (base + "/keys/abcdefghijkl", {"key": key, "label": "stand"}),
        ]:
            response = await http.put(path, json=body, headers=headers)
            response.raise_for_status()
        for route, (_path, definition) in zip(
            fixture["routes"], list(api["paths"].items())[:2], strict=True
        ):
            url = route["path"].replace("{service}", "catalog-service")
            assert (await http.get(url)).status_code == 401
            before = datetime.now(UTC)
            response = await http.get(url, headers={"Authorization": "Bearer " + key})
            response.raise_for_status()
            schema = definition["get"]["responses"]["200"]["content"]["application/json"]["schema"]
            Draft202012Validator({**api, **schema}, format_checker=FormatChecker()).validate(
                response.json()
            )
            if "items" in response.json():
                assert datetime.fromisoformat(response.json()["items"][0]["date"]) >= before
                assert response.json()["next_cursor"]
                assert response.json()["has_more"] is False
            else:
                assert response.json()["status"] == "ok"


def test_conversation_commands_and_localized_receipts_come_from_released_binding():
    binding = yaml.safe_load((DATA / "binding.yaml").read_text())
    conversation = json.loads((INFRA / "stand-conversations/platform-module.json").read_text())
    sends = [step for step in conversation["steps"] if step["action"] == "send"]
    create, listing, show = binding["commands"]
    assert sends[0]["text"].startswith("/" + create["command"] + " ")
    assert sends[1]["text"] == "/" + listing["command"]
    assert sends[2]["text"] == "/" + show["command"]
    for step in (sends[0], sends[-1]):
        language = step["language"]
        assert step["expect"]["exact"] == create["reply"]["parts"][0][language] + "stand_public"
    press = next(step for step in conversation["steps"] if step["action"] == "press")
    assert press["button"] == listing["buttons"][0]["label"][press["language"]]
    wait = next(step for step in conversation["steps"] if step["action"] == "wait")
    assert wait["expect"]["prefix"].startswith(
        binding["events"][0]["reply"]["parts"][0][wait["language"]]
    )
    assert wait["timeout"] >= 120


async def test_released_binding_conversation_runs_ru_en_and_timer_with_settings_readback(
    monkeypatch,
):
    from src.consumers._qa_redaction import QARunRedaction

    binding = yaml.safe_load((DATA / "binding.yaml").read_text())
    conversation = json.loads((INFRA / "stand-conversations/platform-module.json").read_text())
    fixture = json.loads((INFRA / "stand-platform-fixtures/platform-module.json").read_text())
    post = fixture["routes"][1]["body"]["items"][0]
    fields = {
        "channel": post["channel"],
        "date": "2026-10-08T00:00:00+00:00",
        "text": post["text"],
        "url": post["url"],
    }
    language = "ru"
    settings = []
    client = Transport()
    client.get_entity = AsyncMock(return_value=client.bot)
    client.disconnect = AsyncMock()
    create, listing, show = binding["commands"]

    def render(parts):
        return "".join(
            fields[part["source"].rsplit(".", 1)[-1]] if "source" in part else part[language]
            for part in parts
        )

    async def send(bot, text):
        sent = client.message(text, out=True)
        command = text.split()[0].lstrip("/")
        if command == create["command"]:
            client.message(render(create["reply"]["parts"]))
        elif command == listing["command"]:

            async def click():
                listed.raw_text = render(listing["buttons"][0]["reply"]["parts"])

            listed = client.message(
                render(listing["reply_each"]["parts"]),
                buttons=[
                    [SimpleNamespace(text=listing["buttons"][0]["label"][language], click=click)]
                ],
            )
        elif command == show["command"]:
            client.message(render(show["reply_each"]["parts"]))
        else:
            raise AssertionError("command not present in released binding")
        return sent

    async def sleep(seconds):
        client.now += seconds
        if client.now == 75:
            client.message(render(binding["events"][0]["reply"]["parts"]))

    async def request(request):
        nonlocal language
        body = json.loads(request.content)
        settings.append((request.url.path, body["key"], body.get("value")))
        if request.url.path == "/settings/set":
            assert request.headers["X-Settings-Capability"] == "runtime-only-setting-secret"
            language = body["value"]
        return httpx.Response(
            200,
            json={"contract_version": 1, "key": body["key"], "scope": "product", "value": language},
        )

    http_factory = httpx.AsyncClient

    def http(**kwargs):
        return http_factory(**kwargs, transport=httpx.MockTransport(request))

    original_steps = stand_conversation.run_steps

    async def steps(*args, **kwargs):
        return await original_steps(*args, **kwargs, clock=lambda: client.now, sleep=sleep)

    import telethon

    monkeypatch.setenv("LIVE_CONTOUR", "stand")
    monkeypatch.setenv("STAND_CONVERSATION_DIR", str(INFRA / "stand-conversations"))
    monkeypatch.setattr(telethon, "TelegramClient", lambda *args, **kwargs: client)
    from telethon.sessions import StringSession

    monkeypatch.setattr(
        stand_conversation,
        "telethon_env",
        lambda: {
            "TELETHON_SESSION": StringSession().save(),
            "TELETHON_API_ID": "1",
            "TELETHON_API_HASH": "private-test-hash",
        },
    )
    monkeypatch.setattr(stand_conversation, "prove_qa_identity", AsyncMock())
    monkeypatch.setattr(stand_conversation.httpx, "AsyncClient", http)
    monkeypatch.setattr(stand_conversation, "run_steps", steps)
    client.send_message = send
    evidence = {}
    await stand_conversation.run_probe(
        marker="platform-module",
        bot_username="test_bot",
        deployed_url="https://product.example",
        evidence=evidence,
        redaction=QARunRedaction(),
        stored={"SETTINGS_WRITE_CAPABILITY": "runtime-only-setting-secret"},
    )
    assert evidence["status"] == "passed"
    assert evidence["languages"] == {"ru": True, "en": True}
    assert len(evidence["steps"]) == len(conversation["steps"])
    assert evidence["steps"][3]["elapsed_seconds"] == 75
    assert settings[0] == ("/settings/get", "language", None)
    assert ("/settings/set", "language", "en") in settings
    client.disconnect.assert_awaited_once()
