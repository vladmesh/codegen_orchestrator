"""Fresh Telethon clients exercise the adapter with native input-peer conversion.

Only connection and Telegram responses are controlled. Telethon's memory/session
caches and get_input_entity run unchanged, without a socket or real clock sleep.
"""

import asyncio
from types import SimpleNamespace

import pytest
import telethon
from telethon.sessions import StringSession
from telethon.tl.functions.messages import GetBotCallbackAnswerRequest
from telethon.tl.functions.users import GetUsersRequest
from telethon.tl.types import InputPeerUser, User, UserEmpty

from src.synthetic_buyer.config import parse_config
from src.synthetic_buyer.evidence import Redaction
from src.synthetic_buyer.telegram import Peer, TelethonPort, TransportError
from tests.unit.synthetic_buyer.fakes import (
    BOTFATHER,
    BUYER,
    ENVIRON,
    PRODUCT,
    PROMO,
    START,
    USER_ID,
    config_data,
    harness,
)

CODEGEN = Peer(8519991166, "development_bot_vlad_bot", True)
METHODS = ("latest_id", "messages_after", "send", "press")


def _entity(peer):
    return User(id=peer.id, username=peer.username, bot=peer.is_bot, access_hash=peer.id + 9000000)


def _message(message_id, *, outgoing=False):
    return SimpleNamespace(
        id=message_id,
        sender_id=CODEGEN.id,
        out=outgoing,
        date=START,
        raw_text="registered",
    )


@pytest.fixture
def clients(monkeypatch):
    built = []
    entities = {f"@{p.username}": _entity(p) for p in (CODEGEN, BOTFATHER, PRODUCT)}

    class Client(telethon.TelegramClient):
        send_hook = staticmethod(lambda text: None)

        def __init__(self, session, api_id, api_hash, *, receive_updates):
            super().__init__(session, api_id, api_hash, receive_updates=receive_updates)
            self.active = False
            self.updates = receive_updates
            self.effects = []
            self.events = []
            self.resolutions = []
            built.append(self)

        async def connect(self):
            self.active = True

        async def disconnect(self):
            self.active = False

        async def is_user_authorized(self):
            return True

        async def get_me(self):
            self.events.append("buyer_proven")
            return SimpleNamespace(id=BUYER)

        async def get_entity(self, username):
            self.resolutions.append(username)
            entity = entities[username]
            if isinstance(entity, Exception):
                raise entity
            self.session.process_entities([entity])
            return entity

        async def get_messages(self, entity, *, limit, min_id=None):
            target = await self.get_input_entity(entity)
            self.effects.append(("read", target))
            if min_id is None:
                return [_message(41)]
            return [_message(43)] if min_id < 43 else []

        async def send_message(self, entity, text):
            target = await self.get_input_entity(entity)
            self.events.append("send")
            self.effects.append(("send", target))
            type(self).send_hook(text)
            return _message(42, outgoing=True)

        async def __call__(self, request, *args, **kwargs):
            if isinstance(request, GetUsersRequest):
                # Telegram cannot recover this bot from an id and a zero hash.
                return [UserEmpty(id=request.id[0].user_id)]
            assert isinstance(request, GetBotCallbackAnswerRequest)
            await request.resolve(self, telethon.utils)
            self.effects.append(("press", request.peer))
            assert (request.msg_id, request.data) == (43, b"confirm")

    monkeypatch.setattr(telethon, "TelegramClient", Client)
    monkeypatch.setattr("telethon.sessions.StringSession", lambda value: StringSession())
    return built, entities


async def _invoke(port, peer, method):
    if method == "latest_id":
        return await port.latest_id(peer)
    if method == "messages_after":
        return await port.messages_after(peer, 42)
    if method == "send":
        return await port.send(peer, "hello")
    return await port.press(peer, 43, b"confirm")


def _port():
    return TelethonPort(parse_config(config_data()).telegram, ENVIRON, Redaction())


async def test_connected_client_reuses_each_validated_target_across_polls_and_dispatch(clients):
    built, entities = clients
    port = _port()
    await port.connect()
    peers = [await port.resolve(peer.username) for peer in (CODEGEN, BOTFATHER, PRODUCT)]

    async def dispatch_all():
        for peer in peers:
            assert await port.latest_id(peer) == 41
            for _ in range(5):
                assert [message.id for message in await port.messages_after(peer, 42)] == [43]
            assert (await port.send(peer, "hello")).outgoing
            await port.press(peer, 43, b"confirm")

    await dispatch_all()
    assert built[0].resolutions == [f"@{peer.username}" for peer in peers]
    assert built[0].effects == [
        (method, InputPeerUser(peer.id, peer.id + 9000000))
        for peer in peers
        for method in ["read"] * 6 + ["send", "press"]
    ]
    await port.disconnect()

    for entity in entities.values():
        entity.access_hash += 1
    await port.connect()
    await dispatch_all()
    assert built[1].resolutions == [f"@{peer.username}" for peer in peers]
    assert built[1].effects == [
        (method, InputPeerUser(peer.id, peer.id + 9000001))
        for peer in peers
        for method in ["read"] * 6 + ["send", "press"]
    ]
    await port.disconnect()
    assert not port.connected and all(not client.active for client in built)


async def test_controller_reply_polls_resolve_once_on_its_connected_client(clients, tmp_path):
    built, _ = clients
    h = harness(tmp_path)
    h.telegram = _port()
    buyer = h.buyer(codegen_bot={"username": CODEGEN.username, "user_id": CODEGEN.id})
    await buyer._preflight()
    peer = await buyer._codegen()
    await buyer._ensure_watermark(peer, "codegen")

    replies = await buyer._await_reply(peer, "codegen", timeout=60)

    assert [message.id for message in replies] == [43]
    assert len(built) == 3
    assert len(built[2].effects) > 2
    assert built[2].resolutions == [f"@{CODEGEN.username}"]
    assert all(not client.active for client in built) and not h.telegram.connected


@pytest.mark.parametrize("mismatch", ["id", "username", "bot"])
async def test_cached_target_cannot_authorize_a_different_expected_peer(clients, mismatch):
    built, entities = clients
    port = _port()
    await port.connect()
    peer = await port.resolve(CODEGEN.username)
    assert await port.latest_id(peer) == 41
    if mismatch == "id":
        expected = Peer(peer.id + 1, peer.username, peer.is_bot)
    elif mismatch == "username":
        expected = Peer(peer.id, "another_bot", peer.is_bot)
        entities["@another_bot"] = _entity(peer)
    else:
        expected = Peer(peer.id, peer.username, False)
    before = list(built[0].effects)

    for method in METHODS:
        with pytest.raises(TransportError, match="peer identity mismatch"):
            await _invoke(port, expected, method)

    assert built[0].effects == before
    assert await port.latest_id(peer) == 41
    await port.disconnect()


@pytest.mark.parametrize("failure", ["identity", "resolution", "conversion"])
async def test_failed_acquisition_does_not_supply_a_target_to_later_dispatch(
    clients, monkeypatch, failure
):
    built, entities = clients
    port = _port()
    await port.connect()
    if failure == "identity":
        replacement = _entity(CODEGEN)
        replacement.id += 1
        entities[f"@{CODEGEN.username}"] = replacement
    elif failure == "resolution":
        entities[f"@{CODEGEN.username}"] = ValueError("synthetic-secret")
    else:
        original = built[0].get_input_entity

        async def refuse(entity):
            raise ValueError("synthetic-secret")

        monkeypatch.setattr(built[0], "get_input_entity", refuse)

    with pytest.raises(TransportError):
        await port.send(CODEGEN, "hello")
    assert built[0].effects == []
    entities[f"@{CODEGEN.username}"] = _entity(CODEGEN)
    if failure == "conversion":
        monkeypatch.setattr(built[0], "get_input_entity", original)

    assert (await port.send(CODEGEN, "hello")).outgoing
    assert built[0].resolutions == [f"@{CODEGEN.username}"] * 2
    assert built[0].effects == [("send", InputPeerUser(CODEGEN.id, CODEGEN.id + 9000000))]
    await port.disconnect()


async def test_registration_watermark_works_after_validated_codegen_disconnect(clients, tmp_path):
    built, _ = clients
    h = harness(tmp_path)
    h.telegram = _port()
    buyer = h.buyer(codegen_bot={"username": CODEGEN.username, "user_id": CODEGEN.id})
    await buyer._preflight()
    (resolved_client,) = built
    assert not resolved_client.active and not h.telegram.connected

    # Registration's watermark opens a different empty-cache client.
    await buyer._ensure_watermark(await buyer._codegen(), "codegen")

    assert h.store.record["watermarks"]["codegen"] == 41
    assert len(built) == 2
    assert built[1].effects == [("read", InputPeerUser(CODEGEN.id, CODEGEN.id + 9000000))]
    assert all(not client.active and client.updates is False for client in built)
    assert not h.telegram.connected


async def test_registration_redeems_once_with_buyer_proof_and_disconnects_each_phase(
    clients, tmp_path
):
    built, _ = clients
    h = harness(tmp_path)
    h.telegram = _port()
    buyer = h.buyer(codegen_bot={"username": CODEGEN.username, "user_id": CODEGEN.id})
    await buyer._preflight()
    type(built[0]).send_hook = staticmethod(h.world.codegen_answer)

    await buyer._registration()

    assert h.store.record["registration"]["mode"] == "redeemed"
    assert h.store.record["ids"]["user_id"] == USER_ID
    assert h.store.record["pending"] is None
    assert len(h.world.promos) == 1
    assert h.world.promos[0]["redeemed_by_user_id"] == USER_ID
    assert [client.events for client in built] == [
        ["buyer_proven"],
        ["buyer_proven"],
        ["buyer_proven", "send"],
    ]
    assert not h.telegram.connected and all(not client.active for client in built)
    assert PROMO not in h.evidence_text()


@pytest.mark.parametrize(
    "peer", [CODEGEN, BOTFATHER, PRODUCT], ids=["codegen", "botfather", "product"]
)
@pytest.mark.parametrize("method", METHODS)
async def test_each_peer_operation_uses_its_validated_target_on_a_fresh_client(
    clients, peer, method
):
    built, entities = clients
    port = _port()
    await port.connect()
    validated = await port.resolve(peer.username)
    assert await built[0].get_input_entity(peer.id) == InputPeerUser(peer.id, peer.id + 9000000)
    await port.disconnect()
    entities[f"@{peer.username}"].access_hash += 1
    await port.connect()
    fresh = built[1]
    with pytest.raises(ValueError, match="Could not find the input entity"):
        await fresh.get_input_entity(peer.id)

    result = await _invoke(port, validated, method)

    if method == "latest_id":
        assert result == 41
    elif method == "messages_after":
        assert [message.id for message in result] == [43]
    elif method == "send":
        assert (result.id, result.outgoing) == (42, True)
    assert fresh.effects == [
        (
            "read" if method in ("latest_id", "messages_after") else method,
            InputPeerUser(peer.id, peer.id + 9000001),
        )
    ]
    await port.disconnect()
    assert all(not client.active and client.updates is False for client in built)


@pytest.mark.parametrize("method", METHODS)
@pytest.mark.parametrize("mismatch", ["id", "username", "bot"])
@pytest.mark.parametrize("expected", [CODEGEN, BOTFATHER, PRODUCT])
async def test_changed_bot_identity_refuses_before_dispatch(clients, method, mismatch, expected):
    built, entities = clients
    port = _port()
    await port.connect()
    peer = await port.resolve(expected.username)
    await port.disconnect()
    replacement = _entity(expected)
    if mismatch == "id":
        replacement.id += 1
    elif mismatch == "username":
        replacement.username = "another_bot"
    else:
        replacement.bot = False
    entities[f"@{expected.username}"] = replacement
    await port.connect()

    with pytest.raises(TransportError, match="peer identity mismatch"):
        await _invoke(port, peer, method)

    assert built[1].effects == []
    await port.disconnect()


@pytest.mark.parametrize("method", METHODS)
async def test_resolution_that_never_answers_is_bounded_before_dispatch(
    clients, monkeypatch, method
):
    built, _ = clients
    port = _port()
    await port.connect()
    peer = await port.resolve(CODEGEN.username)
    await port.disconnect()
    await port.connect()

    async def stalled(username):
        await asyncio.Event().wait()

    monkeypatch.setattr(built[1], "get_entity", stalled)
    port.timeout = 0
    with pytest.raises(TransportError) as failed:
        await _invoke(port, peer, method)

    assert (failed.value.stage, failed.value.detail) == ("resolve", "did not answer in 0s")
    assert built[1].effects == []
    port.timeout = 30
    await port.disconnect()


@pytest.mark.parametrize("method", METHODS)
async def test_fresh_resolution_failure_has_only_a_safe_classification(clients, method):
    built, entities = clients
    port = _port()
    await port.connect()
    peer = await port.resolve(CODEGEN.username)
    await port.disconnect()
    entities[f"@{CODEGEN.username}"] = ValueError("session-and-access-hash-secret")
    await port.connect()

    with pytest.raises(TransportError) as failed:
        await _invoke(port, peer, method)

    assert (failed.value.stage, failed.value.detail) == ("resolve", "failed: ValueError")
    assert failed.value.__suppress_context__
    assert built[1].effects == []
    await port.disconnect()
