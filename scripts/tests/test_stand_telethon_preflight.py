"""The QA session is proven before a paid suite spends: what each answer means.

The client is a fake with Telethon's async surface; the verdict is what the
workflow step prints and exits on. Every scenario also checks the client was
disconnected, because the session must not stay open on the runner while
qa-worker holds it on the stand.
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import urllib.error

import pytest

from scripts import stand_telethon_preflight as preflight
from scripts.stand_run import SUITES
from shared.contracts.bot_access import QA_TEST_TELEGRAM_ID

REPO = Path(__file__).resolve().parents[2]
BOT = "mega_e2e_codegen_bot"
# Shaped like a Telethon StringSession: a version digit and ~350 base64url chars.
SESSION = "1" + base64.urlsafe_b64encode(os.urandom(263)).decode()
API_HASH = "0123456789abcdef0123456789abcdef"
BOT_TOKEN = "123456789:AAFakeStandProductBotTokenValue000000"  # noqa: S105


class FakeClient:
    def __init__(
        self,
        *,
        authorized: bool = True,
        user_id: int = QA_TEST_TELEGRAM_ID,
        entity: object | Exception | None = None,
        send_error: Exception | None = None,
        connect_error: Exception | None = None,
        hang_on: str | None = None,
    ):
        self.authorized = authorized
        self.user_id = user_id
        self.entity = SimpleNamespace(bot=True, username=BOT) if entity is None else entity
        self.send_error = send_error
        self.connect_error = connect_error
        self.hang_on = hang_on
        self.calls: list[tuple] = []
        self.disconnected = False

    async def _maybe_hang(self, name: str) -> None:
        if self.hang_on == name:
            await asyncio.sleep(3600)

    async def connect(self):
        self.calls.append(("connect",))
        if self.connect_error:
            raise self.connect_error

    async def is_user_authorized(self):
        self.calls.append(("is_user_authorized",))
        return self.authorized

    async def get_me(self):
        self.calls.append(("get_me",))
        return SimpleNamespace(id=self.user_id)

    async def get_entity(self, name):
        self.calls.append(("get_entity", name))
        await self._maybe_hang("get_entity")
        if isinstance(self.entity, Exception):
            raise self.entity
        return self.entity

    async def send_message(self, entity, text):
        self.calls.append(("send_message", entity, text))
        if self.send_error:
            raise self.send_error

    async def disconnect(self):
        self.disconnected = True


def _prove(client: FakeClient) -> preflight.Verdict:
    return asyncio.run(preflight.prove_session(client, bot_username=BOT))


def test_an_authorized_qa_session_that_reaches_the_bot_passes_and_disconnects():
    client = FakeClient()

    verdict = _prove(client)

    assert verdict.refusal is None
    assert verdict.user_id == QA_TEST_TELEGRAM_ID
    assert ("get_entity", f"@{BOT}") in client.calls
    assert ("send_message", client.entity, "/start") in client.calls
    assert client.disconnected
    assert verdict.line() == (
        f"telethon_preflight: pass user_id={QA_TEST_TELEGRAM_ID} bot=@{BOT} "
        f"expected_user_id={QA_TEST_TELEGRAM_ID}"
    )


def test_an_unauthorized_session_is_refused_before_it_is_asked_anything_else():
    client = FakeClient(authorized=False)

    verdict = _prove(client)

    assert verdict.refusal is preflight.Refusal.SESSION_UNAUTHORIZED
    assert verdict.refusal.value == "telethon_session_unauthorized"
    assert [call[0] for call in client.calls] == ["connect", "is_user_authorized"]
    assert client.disconnected
    assert "reason=telethon_session_unauthorized" in verdict.line()


def test_a_session_that_cannot_connect_is_refused_as_unauthorized_naming_only_the_error():
    client = FakeClient(connect_error=ConnectionError(f"refused for {SESSION}"))

    verdict = _prove(client)

    assert verdict.refusal is preflight.Refusal.SESSION_UNAUTHORIZED
    assert verdict.detail == "connect failed: ConnectionError"
    assert SESSION not in verdict.line()
    assert client.disconnected


def test_a_session_of_another_account_is_refused_as_an_identity_mismatch():
    client = FakeClient(user_id=QA_TEST_TELEGRAM_ID + 1)

    verdict = _prove(client)

    assert verdict.refusal is preflight.Refusal.IDENTITY_MISMATCH
    assert verdict.refusal.value == "telethon_identity_mismatch"
    assert verdict.user_id == QA_TEST_TELEGRAM_ID + 1
    assert not any(call[0] == "send_message" for call in client.calls)
    assert client.disconnected
    line = verdict.line()
    assert f"user_id={QA_TEST_TELEGRAM_ID + 1}" in line
    assert f"expected_user_id={QA_TEST_TELEGRAM_ID}" in line


@pytest.mark.parametrize(
    ("client", "detail"),
    [
        (FakeClient(entity=ValueError("No user has that username")), "resolve failed: ValueError"),
        (FakeClient(entity=SimpleNamespace(bot=False)), "the username does not resolve to a bot"),
        (
            FakeClient(send_error=PermissionError("You blocked this bot")),
            "/start failed: PermissionError",
        ),
    ],
    ids=["unresolvable", "not-a-bot", "cannot-write"],
)
def test_a_bot_the_session_cannot_reach_is_refused_as_unreachable(client, detail):
    verdict = _prove(client)

    assert verdict.refusal is preflight.Refusal.BOT_UNREACHABLE
    assert verdict.refusal.value == "telethon_bot_unreachable"
    assert verdict.detail == detail
    assert verdict.user_id == QA_TEST_TELEGRAM_ID
    assert client.disconnected
    assert f"bot=@{BOT}" in verdict.line()


def test_a_hung_telegram_call_is_a_named_refusal_not_a_wait(monkeypatch):
    monkeypatch.setattr(preflight, "CALL_TIMEOUT_SECONDS", 0.01)
    client = FakeClient(hang_on="get_entity")

    verdict = _prove(client)

    assert verdict.refusal is preflight.Refusal.BOT_UNREACHABLE
    assert verdict.detail == "resolve did not answer in 0.01s"
    assert client.disconnected


def _environment(**overrides: str) -> dict[str, str]:
    environment = {
        "TELETHON_API_ID": "12345",
        "TELETHON_API_HASH": API_HASH,
        "TELETHON_SESSION": SESSION,
        "STAND_PRODUCT_BOT_TOKEN": BOT_TOKEN,
    }
    environment.update(overrides)
    return environment


def test_prove_builds_the_client_from_the_stand_secrets_and_names_the_bot_from_its_token():
    seen: dict[str, object] = {}
    client = FakeClient()

    def factory(environment):
        seen["environment"] = dict(environment)
        return client

    def lookup(token):
        seen["token"] = token
        return BOT

    verdict = preflight.prove(_environment(), client_factory=factory, bot_lookup=lookup)

    assert verdict.refusal is None
    assert seen["token"] == BOT_TOKEN
    assert seen["environment"]["TELETHON_SESSION"] == SESSION
    assert client.disconnected


@pytest.mark.parametrize("missing", preflight.TELETHON_ENV_VARS)
def test_prove_refuses_missing_credentials_by_name_without_building_a_client(missing):
    def factory(_environment):
        raise AssertionError("no client without credentials")

    verdict = preflight.prove(
        _environment(**{missing: ""}), client_factory=factory, bot_lookup=lambda _t: BOT
    )

    assert verdict.refusal is preflight.Refusal.SESSION_UNAUTHORIZED
    assert verdict.detail == f"missing {missing}"


def test_prove_refuses_a_bot_it_cannot_name_before_opening_the_session():
    def factory(_environment):
        raise AssertionError("the session is not opened for a bot nobody can name")

    verdict = preflight.prove(_environment(STAND_PRODUCT_BOT_TOKEN=""), client_factory=factory)

    assert verdict.refusal is preflight.Refusal.BOT_UNREACHABLE
    assert verdict.detail == "STAND_PRODUCT_BOT_TOKEN is not set"


def test_prove_refuses_a_session_string_telethon_cannot_load():
    def factory(_environment):
        raise ValueError(f"Not a valid string: {SESSION}")

    verdict = preflight.prove(_environment(), client_factory=factory, bot_lookup=lambda _t: BOT)

    assert verdict.refusal is preflight.Refusal.SESSION_UNAUTHORIZED
    assert verdict.detail == "the session could not be loaded: ValueError"
    assert SESSION not in verdict.line()


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


def test_the_bot_username_is_what_the_bot_api_getme_reports():
    requested: list[str] = []

    def opener(request, timeout):
        requested.append(request.full_url)
        return _Response(json.dumps({"ok": True, "result": {"username": BOT}}).encode())

    assert preflight.product_bot_username(BOT_TOKEN, opener=opener) == BOT
    assert requested == [f"https://api.telegram.org/bot{BOT_TOKEN}/getMe"]


@pytest.mark.parametrize(
    ("error", "detail"),
    [
        (
            urllib.error.HTTPError(
                f"https://api.telegram.org/bot{BOT_TOKEN}/getMe", 401, "Unauthorized", {}, None
            ),
            "Bot API getMe answered HTTP 401",
        ),
        (
            urllib.error.URLError(f"https://api.telegram.org/bot{BOT_TOKEN}/getMe"),
            "Bot API getMe failed: URLError",
        ),
    ],
    ids=["rejected-token", "no-network"],
)
def test_a_bot_api_failure_is_unreachable_and_never_echoes_the_token(error, detail):
    def opener(_request, timeout):
        raise error

    with pytest.raises(preflight._Refused) as refused:
        preflight.product_bot_username(BOT_TOKEN, opener=opener)

    assert refused.value.refusal is preflight.Refusal.BOT_UNREACHABLE
    assert refused.value.detail == detail
    assert BOT_TOKEN not in refused.value.detail


def test_a_getme_answer_without_a_username_is_unreachable():
    def opener(_request, timeout):
        return _Response(json.dumps({"ok": False, "description": "Unauthorized"}).encode())

    with pytest.raises(preflight._Refused) as refused:
        preflight.product_bot_username(BOT_TOKEN, opener=opener)

    assert refused.value.detail == "Bot API getMe returned no bot username"


def test_the_prove_command_exits_nonzero_with_the_reason_and_no_credential(monkeypatch, capsys):
    monkeypatch.setattr(
        preflight,
        "prove",
        lambda _environment: preflight.Verdict(
            preflight.Refusal.IDENTITY_MISMATCH, "wrong account", 42, BOT
        ),
    )

    assert preflight.main(["prove"]) == 1
    captured = capsys.readouterr()
    assert "reason=telethon_identity_mismatch user_id=42" in captured.err
    assert captured.out == ""


def test_only_suites_whose_qa_executor_judges_the_bot_product_need_the_session(capsys):
    assert preflight.QA_TELETHON_SUITES == {
        name for name, suite in SUITES.items() if suite.llm and suite.telegram_bot_product
    }
    assert preflight.needs_session("mega-live")
    for suite in ("mega-noop", "mega-brief", "mega-brief-package", "tests/live/test_x.py"):
        assert not preflight.needs_session(suite)

    assert preflight.main(["needs-session", "--suite", "mega-live"]) == 0
    assert preflight.main(["needs-session", "--suite", "mega-noop"]) == 0
    assert capsys.readouterr().out == "true\nfalse\n"


def test_needs_session_runs_on_a_bare_python_without_telethon_or_pydantic():
    """The workflow's suite resolution asks it with the runner's bare `python3`."""
    probe = (
        "import sys; import scripts.stand_telethon_preflight; "
        "print(sorted(m for m in ('telethon', 'pydantic', 'yaml') if m in sys.modules))"
    )
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-c", probe],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=True,
    )

    assert result.stdout.strip() == "[]"
