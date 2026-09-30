"""PO persistence regressions: read every payload from a real PostgreSQL database."""

from __future__ import annotations

import asyncio
import logging
import os
from unittest.mock import AsyncMock

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import tool
import psycopg
from psycopg.rows import dict_row
from pydantic import Field
import pytest
import structlog

from shared.contracts.queues.po import POUserMessage, po_thread_id
from src.agents.po.graph import create_po_graph
from src.consumers.po import _handle_message, _process_message, _repair_orphan_tool_calls

CANARIES = (
    "1234567890:AAcheckpoint_test_token_abcdefgh",
    "sk-or-v1-checkpoint_test_provider_abcdefgh",
    "GOCSPX-checkpoint_test_oauth_abcdefgh",
    "wqzj mvnp frtk bxhs",
)
SECRET_TEXT = " / ".join(CANARIES)
TEST_KEY = "wHhIQWmPfLt60oHdxzbQhY1ZKnUon12e5_SuZ33xDxc="
CHAT = "14280001"
CONFIG = {"configurable": {"thread_id": po_thread_id(CHAT)}}


class ScriptedModel(BaseChatModel):
    turns: list[AIMessage]
    inputs: list[list[BaseMessage]] = Field(default_factory=list)
    failure: str | None = None

    @property
    def _llm_type(self) -> str:
        return "checkpoint-fixture"

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.inputs.append(list(messages))
        if self.failure:
            raise RuntimeError(self.failure)
        return ChatResult(generations=[ChatGeneration(message=self.turns.pop(0))])


@tool
def repeat_secret(value: str) -> str:
    """Deterministically repeat tool arguments into the result."""
    return value


@pytest.fixture
async def checkpoint_db(monkeypatch):
    url = os.environ["CHECKPOINT_TEST_DATABASE_URL"]
    monkeypatch.setenv("SECRETS_ENCRYPTION_KEY", TEST_KEY)
    async with await psycopg.AsyncConnection.connect(url, autocommit=True) as conn:
        await conn.execute("DROP SCHEMA IF EXISTS langgraph CASCADE")
        await conn.execute("CREATE SCHEMA langgraph")
    yield url


@pytest.fixture(autouse=True)
def safe_logs(caplog, capsys):
    # Render exceptions as production JSON logging does. Capturing only raw
    # structlog dictionaries would miss a payload echoed in an exc_info trace.
    previous = structlog.get_config()
    events = []

    def collect(logger, method_name, event_dict):
        events.append(event_dict.copy())
        return event_dict

    structlog.configure(
        processors=[
            collect,
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=False,
    )
    caplog.set_level(logging.DEBUG)
    try:
        yield events
    finally:
        structlog.configure(**previous)
        captured = capsys.readouterr()
        for secret in CANARIES:
            assert secret not in str(events) + caplog.text + captured.out + captured.err


@pytest.fixture(autouse=True)
def consumer_side_effects(monkeypatch):
    # Redis dialogue transport is not a history store. These time/event-bookkeeping
    # calls are unrelated to the PostgreSQL boundary under test.
    monkeypatch.setattr("src.consumers.po._record_user_message", AsyncMock())
    monkeypatch.setattr("src.consumers.po.remember_owner_event", AsyncMock())


async def stored_rows(url):
    async with await psycopg.AsyncConnection.connect(url, row_factory=dict_row) as conn:
        return {
            table: await (await conn.execute(f"SELECT * FROM {table}")).fetchall()
            for table in (
                "checkpoints",
                "checkpoint_blobs",
                "checkpoint_writes",
                "checkpoint_migrations",
            )
        }


def assert_no_secrets(rows):
    # Inspect every column, including JSONB metadata, inline state and BYTEA blobs.
    for records in rows.values():
        for row in records:
            for value in row.values():
                raw = bytes(value) if isinstance(value, bytes | memoryview) else str(value).encode()
                for secret in CANARIES:
                    assert secret.encode() not in raw


async def test_consumer_tool_results_and_resume_are_encrypted(checkpoint_db, monkeypatch):
    monkeypatch.setattr("src.agents.po.graph.get_all_tools", lambda: [repeat_secret])
    model = ScriptedModel(
        turns=[
            AIMessage(
                content="",
                tool_calls=[
                    {"name": "repeat_secret", "args": {"value": SECRET_TEXT}, "id": "repeat-1"}
                ],
            ),
            AIMessage(content="Saved."),
        ]
    )
    graph = await create_po_graph(model, model, checkpoint_database_url=checkpoint_db)
    client = AsyncMock()
    message = POUserMessage(text=SECRET_TEXT, telegram_chat_id=CHAT, request_id="request-1")
    try:
        await _handle_message(graph, client, CHAT, message.model_dump(mode="json"))
        rows = await stored_rows(checkpoint_db)
        assert rows["checkpoints"] and rows["checkpoint_blobs"] and rows["checkpoint_writes"]
        assert_no_secrets(rows)
        saved = await graph.aget_state(CONFIG)
        assert any(m.content == SECRET_TEXT for m in saved.values["messages"])
        model.turns.append(AIMessage(content="Resumed."))
        # A fresh graph/pool reads the same history, as a restarted consumer does.
        resumed = await create_po_graph(model, model, checkpoint_database_url=checkpoint_db)
        try:
            await _handle_message(
                resumed,
                client,
                CHAT,
                POUserMessage(
                    text="Continue.", telegram_chat_id=CHAT, request_id="request-2"
                ).model_dump(mode="json"),
            )
            assert any(m.content == SECRET_TEXT for m in model.inputs[-1])
        finally:
            await resumed.checkpointer.conn.close()
        assert_no_secrets(await stored_rows(checkpoint_db))
    finally:
        await graph.checkpointer.conn.close()


@pytest.mark.parametrize("version", [2, 4])
async def test_upgrade_preserves_released_rows_and_pending_work(checkpoint_db, version):
    from langchain_core.messages import HumanMessage
    from langgraph.checkpoint.base import empty_checkpoint
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

    from src.agents.po.checkpoint_upgrade import upgrade
    from src.agents.po.graph import _create_postgres_checkpointer

    config = {"configurable": {"thread_id": "released-thread", "checkpoint_ns": ""}}
    async with AsyncPostgresSaver.from_conn_string(checkpoint_db) as legacy:
        await legacy.setup()
        checkpoint = empty_checkpoint()
        checkpoint["v"] = version
        checkpoint["channel_values"] = {
            "messages": [HumanMessage(content=SECRET_TEXT)],
            "opaque": SECRET_TEXT,
            "bytes": bytearray(CANARIES[0].encode()),
        }
        checkpoint["channel_versions"] = {"messages": "1", "opaque": "1", "bytes": "1"}
        saved_config = await legacy.aput(
            config,
            checkpoint,
            {"source": "input", "step": -1, "echo": SECRET_TEXT},
            checkpoint["channel_versions"],
        )
        await legacy.aput_writes(saved_config, [("messages", SECRET_TEXT)], "task-1")
        before = await legacy.aget_tuple(config)
    original_rows = await stored_rows(checkpoint_db)
    dry = await asyncio.to_thread(upgrade, checkpoint_db, writers_quiesced=True)
    assert dry["before"] == dry["after"]
    assert await stored_rows(checkpoint_db) == original_rows
    result = await asyncio.to_thread(upgrade, checkpoint_db, writers_quiesced=True, apply=True)
    assert sum(c["plaintext"] for c in result["after"].values()) == 0
    assert_no_secrets(await stored_rows(checkpoint_db))
    protected = await _create_postgres_checkpointer(checkpoint_db)
    try:
        after = await protected.aget_tuple(config)
        assert after == before
        history = [c async for c in protected.alist(config, filter={"echo": SECRET_TEXT})]
        assert history == [before]
    finally:
        await protected.conn.close()
    rows = await stored_rows(checkpoint_db)
    rerun = await asyncio.to_thread(upgrade, checkpoint_db, writers_quiesced=True, apply=True)
    assert all(n == 0 for n in rerun["converted"].values())
    assert await stored_rows(checkpoint_db) == rows


async def test_inline_state_and_metadata_are_encrypted_before_write(checkpoint_db):
    from langgraph.checkpoint.base import empty_checkpoint

    from src.agents.po.graph import _create_postgres_checkpointer

    saver = await _create_postgres_checkpointer(checkpoint_db)
    config = {
        "configurable": {
            "thread_id": "metadata-thread",
            "checkpoint_ns": "",
            "user_name": SECRET_TEXT,
        }
    }
    checkpoint = empty_checkpoint()
    checkpoint["channel_values"] = {"primitive": SECRET_TEXT}
    checkpoint["channel_versions"] = {"primitive": "1"}
    try:
        await saver.aput(
            config,
            checkpoint,
            {"source": "input", "step": -1, "echo": {"input": SECRET_TEXT}},
            checkpoint["channel_versions"],
        )
        assert_no_secrets(await stored_rows(checkpoint_db))
        loaded = await saver.aget_tuple(config)
        assert loaded.checkpoint["channel_values"]["primitive"] == SECRET_TEXT
        assert loaded.metadata["user_name"] == SECRET_TEXT
        assert loaded.metadata["echo"]["input"] == SECRET_TEXT
    finally:
        await saver.conn.close()


async def test_real_summary_hook_persists_and_resumes_secret_summary(checkpoint_db, monkeypatch):
    monkeypatch.setattr("src.agents.po.graph.get_all_tools", lambda: [])
    model = ScriptedModel(turns=[AIMessage(content="Saved."), AIMessage(content="Resumed.")])
    summarizer = ScriptedModel(turns=[AIMessage(content=SECRET_TEXT) for _ in range(6)])
    graph = await create_po_graph(
        model,
        summarizer,
        checkpoint_database_url=checkpoint_db,
        summarization_max_tokens=1000,
        summarization_trigger_tokens=50,
        summarization_max_summary_tokens=100,
    )
    try:
        await graph.ainvoke(
            {"messages": [HumanMessage(content=SECRET_TEXT + " filler" * 100)]}, CONFIG
        )
        assert summarizer.inputs
        state = await graph.aget_state(CONFIG)
        assert state.values["context"]["running_summary"].summary == SECRET_TEXT
        await graph.ainvoke({"messages": [HumanMessage(content="Continue.")]}, CONFIG)
        assert any(SECRET_TEXT in str(m.content) for m in model.inputs[-1])
        assert_no_secrets(await stored_rows(checkpoint_db))
    finally:
        await graph.checkpointer.conn.close()


async def test_orphan_repair_and_retry_use_the_same_encrypted_saver(checkpoint_db, monkeypatch):
    monkeypatch.setattr("src.agents.po.graph.get_all_tools", lambda: [repeat_secret])
    call = {"name": "repeat_secret", "args": {"value": SECRET_TEXT}, "id": "orphan-1"}
    model = ScriptedModel(
        turns=[AIMessage(content="", tool_calls=[call]), AIMessage(content="Recovered.")]
    )
    graph = await create_po_graph(model, model, checkpoint_database_url=checkpoint_db)
    try:
        graph.interrupt_before_nodes = ["tools"]
        await graph.ainvoke({"messages": [HumanMessage(content=SECRET_TEXT)]}, CONFIG)
        graph.interrupt_before_nodes = []
        assert await _repair_orphan_tool_calls(graph, po_thread_id(CHAT)) == 1
        assert await _repair_orphan_tool_calls(graph, po_thread_id(CHAT)) == 0
        state = await graph.aget_state(CONFIG)
        assert any(
            isinstance(m, ToolMessage) and m.tool_call_id == "orphan-1"
            for m in state.values["messages"]
        )
        native_invoke = graph.ainvoke
        attempts = 0

        async def race(input, config):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise ValueError(
                    "tool_calls that do not have a corresponding ToolMessage " + SECRET_TEXT
                )
            return await native_invoke(input, config)

        monkeypatch.setattr(graph, "ainvoke", race)
        await _handle_message(
            graph,
            AsyncMock(),
            CHAT,
            POUserMessage(
                text="Retry " + SECRET_TEXT, telegram_chat_id=CHAT, request_id="retry-1"
            ).model_dump(mode="json"),
        )
        assert attempts == 2
        assert_no_secrets(await stored_rows(checkpoint_db))
    finally:
        await graph.checkpointer.conn.close()


async def test_payload_bearing_model_error_is_not_logged(checkpoint_db, monkeypatch, safe_logs):
    monkeypatch.setattr("src.agents.po.graph.get_all_tools", lambda: [])
    model = ScriptedModel(turns=[], failure=SECRET_TEXT)
    graph = await create_po_graph(model, model, checkpoint_database_url=checkpoint_db)
    try:
        await _process_message(
            graph,
            AsyncMock(),
            asyncio.Semaphore(1),
            {},
            "entry-1",
            POUserMessage(text=SECRET_TEXT, telegram_chat_id=CHAT, request_id="failure-1"),
        )
        assert any(e["event"] == "po_invoke_failed" for e in safe_logs)
        assert_no_secrets(await stored_rows(checkpoint_db))
    finally:
        await graph.checkpointer.conn.close()


@pytest.mark.parametrize("key", [None, SECRET_TEXT], ids=["missing", "invalid"])
async def test_invalid_key_fails_before_setup_or_payload_write(checkpoint_db, monkeypatch, key):
    from src.agents.po.checkpoints import CheckpointProtectionError
    from src.agents.po.graph import _create_postgres_checkpointer

    if key is None:
        monkeypatch.delenv("SECRETS_ENCRYPTION_KEY")
    else:
        monkeypatch.setenv("SECRETS_ENCRYPTION_KEY", key)
    with pytest.raises(CheckpointProtectionError, match="valid SECRETS_ENCRYPTION_KEY") as caught:
        await _create_postgres_checkpointer(checkpoint_db)
    assert all(secret not in str(caught.value) for secret in CANARIES)
    async with await psycopg.AsyncConnection.connect(checkpoint_db) as conn:
        result = await conn.execute(
            "SELECT count(*) FROM information_schema.tables WHERE table_schema='langgraph'"
        )
        assert (await result.fetchone())[0] == 0


@pytest.mark.parametrize(
    "payload", ["checkpoint", "metadata", "checkpoint_blobs", "checkpoint_writes"]
)
async def test_invalid_ciphertext_fails_without_new_writes(checkpoint_db, monkeypatch, payload):
    from psycopg.types.json import Jsonb

    from src.agents.po.checkpoints import ENVELOPE

    monkeypatch.setattr("src.agents.po.graph.get_all_tools", lambda: [])
    model = ScriptedModel(turns=[AIMessage(content="Saved.")])
    graph = await create_po_graph(model, model, checkpoint_database_url=checkpoint_db)
    try:
        await graph.ainvoke({"messages": [HumanMessage(content=SECRET_TEXT)]}, CONFIG)
        if payload == "checkpoint_writes":
            latest = await graph.aget_state(CONFIG)
            await graph.checkpointer.aput_writes(
                latest.config, [("__error__", SECRET_TEXT)], "pending"
            )
        async with await psycopg.AsyncConnection.connect(checkpoint_db, autocommit=True) as conn:
            if payload in {"checkpoint", "metadata"}:
                invalid = {ENVELOPE: {"type": "msgpack+fernet", "ciphertext": SECRET_TEXT}}
                if payload == "checkpoint":
                    await conn.execute(
                        """
                        UPDATE checkpoints SET checkpoint =
                        (checkpoint - %s) || %s WHERE checkpoint_id =
                        (SELECT max(checkpoint_id) FROM checkpoints)
                    """,
                        (ENVELOPE, Jsonb(invalid)),
                    )
                else:
                    await conn.execute("UPDATE checkpoints SET metadata = %s", (Jsonb(invalid),))
            else:
                await conn.execute(f"UPDATE {payload} SET blob = %s", (SECRET_TEXT.encode(),))
        before = await stored_rows(checkpoint_db)
        await _process_message(
            graph,
            AsyncMock(),
            asyncio.Semaphore(1),
            {},
            "bad-cipher",
            POUserMessage(text=SECRET_TEXT, telegram_chat_id=CHAT, request_id="bad-cipher-request"),
        )
        assert await stored_rows(checkpoint_db) == before
    finally:
        await graph.checkpointer.conn.close()


async def test_failed_upgrade_rolls_back_every_table_and_thread(checkpoint_db, monkeypatch):
    from langgraph.checkpoint.base import empty_checkpoint
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

    from src.agents.po.checkpoint_upgrade import upgrade
    from src.agents.po.checkpoints import CheckpointProtectionError
    from src.agents.po.graph import _create_postgres_checkpointer

    async with AsyncPostgresSaver.from_conn_string(checkpoint_db) as legacy:
        await legacy.setup()
        for thread in ("first", "second"):
            checkpoint = empty_checkpoint()
            checkpoint["channel_values"] = {"messages": [HumanMessage(content=SECRET_TEXT)]}
            checkpoint["channel_versions"] = {"messages": "1"}
            config = await legacy.aput(
                {"configurable": {"thread_id": thread, "checkpoint_ns": ""}},
                checkpoint,
                {"source": "input", "step": -1},
                checkpoint["channel_versions"],
            )
            await legacy.aput_writes(config, [("messages", SECRET_TEXT)], "task-1")
    async with await psycopg.AsyncConnection.connect(checkpoint_db, autocommit=True) as conn:
        await conn.execute(
            "UPDATE checkpoint_writes SET blob = %s WHERE thread_id = 'second'",
            (b"\xc1" + SECRET_TEXT.encode(),),
        )
    before = await stored_rows(checkpoint_db)
    with pytest.raises(CheckpointProtectionError, match="quiesced"):
        await asyncio.to_thread(upgrade, checkpoint_db, writers_quiesced=False, apply=True)
    with pytest.raises(CheckpointProtectionError, match="quiesced upgrade"):
        await _create_postgres_checkpointer(checkpoint_db)
    with pytest.raises(CheckpointProtectionError, match="rolled back") as caught:
        await asyncio.to_thread(upgrade, checkpoint_db, writers_quiesced=True, apply=True)
    assert all(secret not in str(caught.value) for secret in CANARIES)
    assert await stored_rows(checkpoint_db) == before


async def test_released_interrupted_conversation_resumes_after_upgrade(checkpoint_db, monkeypatch):
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

    from src.agents.po.checkpoint_upgrade import upgrade

    monkeypatch.setattr("src.agents.po.graph.get_all_tools", lambda: [repeat_secret])
    model = ScriptedModel(
        turns=[
            AIMessage(
                content="",
                tool_calls=[
                    {"name": "repeat_secret", "args": {"value": SECRET_TEXT}, "id": "pending-1"}
                ],
            ),
            AIMessage(content="Finished after upgrade."),
        ]
    )
    async with AsyncPostgresSaver.from_conn_string(checkpoint_db) as legacy:
        await legacy.setup()
        with monkeypatch.context() as released:
            released.setattr(
                "src.agents.po.graph._create_postgres_checkpointer", AsyncMock(return_value=legacy)
            )
            old_graph = await create_po_graph(model, model, checkpoint_database_url=checkpoint_db)
        old_graph.interrupt_before_nodes = ["tools"]
        await old_graph.ainvoke({"messages": [HumanMessage(content=SECRET_TEXT)]}, CONFIG)
        old_state = await old_graph.aget_state(CONFIG)
        assert old_state.next == ("tools",)
        old_history = [c async for c in legacy.alist(CONFIG)]
    await asyncio.to_thread(upgrade, checkpoint_db, writers_quiesced=True, apply=True)
    graph = await create_po_graph(model, model, checkpoint_database_url=checkpoint_db)
    try:
        state = await graph.aget_state(CONFIG)
        assert state.values == old_state.values
        assert state.next == old_state.next
        assert [c async for c in graph.checkpointer.alist(CONFIG)] == old_history
        result = await graph.ainvoke(None, CONFIG)
        assert result["messages"][-1].content == "Finished after upgrade."
        assert any(
            isinstance(m, ToolMessage) and m.content == SECRET_TEXT for m in result["messages"]
        )
        assert_no_secrets(await stored_rows(checkpoint_db))
    finally:
        await graph.checkpointer.conn.close()


async def test_upgrade_refuses_an_active_writer(checkpoint_db):
    from src.agents.po.checkpoint_upgrade import upgrade
    from src.agents.po.checkpoints import CheckpointProtectionError
    from src.agents.po.graph import _create_postgres_checkpointer

    saver = await _create_postgres_checkpointer(checkpoint_db)
    await saver.conn.close()
    async with await psycopg.AsyncConnection.connect(checkpoint_db) as writer:
        await writer.execute("LOCK TABLE checkpoints IN ROW EXCLUSIVE MODE")
        with pytest.raises(CheckpointProtectionError, match="rolled back"):
            await asyncio.to_thread(upgrade, checkpoint_db, writers_quiesced=True, apply=True)


async def test_wrong_key_cannot_resume_or_rerun_upgrade(checkpoint_db, monkeypatch):
    from src.agents.po.checkpoint_upgrade import upgrade
    from src.agents.po.checkpoints import CheckpointProtectionError

    monkeypatch.setattr("src.agents.po.graph.get_all_tools", lambda: [])
    model = ScriptedModel(turns=[AIMessage(content="Saved.")])
    graph = await create_po_graph(model, model, checkpoint_database_url=checkpoint_db)
    try:
        await graph.ainvoke({"messages": [HumanMessage(content=SECRET_TEXT)]}, CONFIG)
    finally:
        await graph.checkpointer.conn.close()
    before = await stored_rows(checkpoint_db)
    monkeypatch.setenv("SECRETS_ENCRYPTION_KEY", "eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHg=")
    graph = await create_po_graph(model, model, checkpoint_database_url=checkpoint_db)
    try:
        with pytest.raises(CheckpointProtectionError, match="decryption failed"):
            await graph.aget_state(CONFIG)
        with pytest.raises(CheckpointProtectionError, match="decryption failed"):
            await asyncio.to_thread(upgrade, checkpoint_db, writers_quiesced=True, apply=True)
        assert await stored_rows(checkpoint_db) == before
        assert_no_secrets(before)
    finally:
        await graph.checkpointer.conn.close()


async def test_empty_ciphertext_is_rejected_even_for_null_pending_writes(checkpoint_db):
    from langgraph.checkpoint.base import empty_checkpoint

    from src.agents.po.checkpoints import CheckpointProtectionError
    from src.agents.po.graph import _create_postgres_checkpointer

    saver = await _create_postgres_checkpointer(checkpoint_db)
    config = {"configurable": {"thread_id": "null-thread", "checkpoint_ns": ""}}
    try:
        config = await saver.aput(config, empty_checkpoint(), {"source": "input", "step": -1}, {})
        await saver.aput_writes(config, [("__error__", None)], "null-task")
        async with await psycopg.AsyncConnection.connect(checkpoint_db, autocommit=True) as conn:
            await conn.execute("UPDATE checkpoint_writes SET blob = %s", (b"",))
        with pytest.raises(CheckpointProtectionError, match="decryption failed"):
            await saver.aget_tuple(config)
    finally:
        await saver.conn.close()


@pytest.mark.parametrize("method", ["aput", "aput_writes"])
async def test_serialization_failure_writes_no_partial_payloads(checkpoint_db, method):
    from langgraph.checkpoint.base import empty_checkpoint

    from src.agents.po.checkpoints import CheckpointProtectionError
    from src.agents.po.graph import _create_postgres_checkpointer

    class Unserializable:
        def __repr__(self):
            return SECRET_TEXT

    saver = await _create_postgres_checkpointer(checkpoint_db)
    config = {"configurable": {"thread_id": "serialize-thread", "checkpoint_ns": ""}}
    try:
        config = await saver.aput(config, empty_checkpoint(), {"source": "input", "step": -1}, {})
        before = await stored_rows(checkpoint_db)
        with pytest.raises(CheckpointProtectionError, match="encryption failed") as caught:
            if method == "aput":
                checkpoint = empty_checkpoint()
                checkpoint["channel_values"] = {"first": [SECRET_TEXT], "second": Unserializable()}
                checkpoint["channel_versions"] = {"first": "1", "second": "1"}
                await saver.aput(
                    config,
                    checkpoint,
                    {"source": "input", "step": -1},
                    checkpoint["channel_versions"],
                )
            else:
                await saver.aput_writes(
                    config, [("first", SECRET_TEXT), ("second", Unserializable())], "task"
                )
        assert all(secret not in str(caught.value) for secret in CANARIES)
        assert await stored_rows(checkpoint_db) == before
        assert_no_secrets(before)
    finally:
        await saver.conn.close()


async def test_maintenance_cli_emits_only_counts_and_safe_errors(
    checkpoint_db, monkeypatch, safe_logs
):
    import sys

    from src.agents.po.checkpoint_upgrade import main
    from src.agents.po.graph import _create_postgres_checkpointer

    saver = await _create_postgres_checkpointer(checkpoint_db)
    await saver.conn.close()
    monkeypatch.setenv("CHECKPOINT_DATABASE_URL", checkpoint_db)
    monkeypatch.setattr(sys, "argv", ["checkpoint_upgrade", "--writers-quiesced"])
    # Keep the production CLI's logging configuration from replacing test capture.
    monkeypatch.setattr("src.agents.po.checkpoint_upgrade.setup_logging", lambda **kwargs: None)
    assert await asyncio.to_thread(main) == 0
    report = next(e for e in safe_logs if e["event"] == "po_checkpoint_upgrade_counts")
    assert report["mode"] == "dry-run"
    assert all(c["plaintext"] == 0 for c in report["after"].values())
    monkeypatch.setenv("SECRETS_ENCRYPTION_KEY", SECRET_TEXT)
    assert await asyncio.to_thread(main) == 1


async def test_existing_secret_tools_through_po_and_real_api(checkpoint_db, monkeypatch):
    import httpx

    from shared.clients.internal_api import InternalAPIClient
    from shared.crypto import decrypt_dict
    from src.agents.po.tools_projects import set_project_secret, validate_telegram_token
    from src.agents.po.tools_shared import init_po_clients

    api = InternalAPIClient(os.environ["API_BASE_URL"])
    headers = {"X-Telegram-ID": CHAT}
    async with httpx.AsyncClient(
        base_url=api.base_url, headers={"X-Internal-Key": os.environ["INTERNAL_API_KEY"]}
    ) as transport:
        user = await transport.get(f"/api/users/by-telegram/{CHAT}")
        if user.status_code == 404:
            user = await transport.post(
                "/api/users/",
                json={
                    "telegram_id": int(CHAT),
                    "username": "checkpoint_fixture",
                    "first_name": "Test",
                },
            )
        user.raise_for_status()
        project = await transport.post(
            "/api/projects/",
            headers=headers,
            json={
                "title": "checkpoint-secret-fixture",
                "initiating_run_id": "checkpoint-fixture-1428",
            },
        )
        project.raise_for_status()
    project_id = project.json()["id"]
    secret_keys = ("PROVIDER_API_KEY", "OAUTH_CLIENT_SECRET", "MAIL_APP_PASSWORD")
    calls = [
        {
            "name": "set_project_secret",
            "args": {"project_id": project_id, "key": key, "value": value},
            "id": f"set-{key}",
        }
        for key, value in zip(secret_keys, CANARIES[1:], strict=True)
    ]
    calls += [
        {
            "name": "set_project_secret",
            "args": {"project_id": project_id, "key": "BOT_TOKEN", "value": CANARIES[0]},
            "id": "refuse-bot",
        },
        {
            "name": "validate_telegram_token",
            "args": {"project_id": project_id, "token": CANARIES[-1]},
            "id": "reject-malformed-token",
        },
    ]
    monkeypatch.setattr(
        "src.agents.po.graph.get_all_tools", lambda: [set_project_secret, validate_telegram_token]
    )
    model = ScriptedModel(
        turns=[AIMessage(content="", tool_calls=calls), AIMessage(content="Saved.")]
    )
    init_po_clients(api, AsyncMock())
    graph = await create_po_graph(model, model, checkpoint_database_url=checkpoint_db)
    try:
        await _handle_message(
            graph,
            AsyncMock(),
            CHAT,
            POUserMessage(
                text=SECRET_TEXT, telegram_chat_id=CHAT, request_id="secret-tools-1"
            ).model_dump(mode="json"),
        )
        state = await graph.aget_state(CONFIG)
        results = {
            m.tool_call_id: m.content
            for m in state.values["messages"]
            if isinstance(m, ToolMessage)
        }
        assert all(results[f"set-{key}"].startswith("Secret '") for key in secret_keys)
        assert results["refuse-bot"].startswith("Error:")
        assert results["reject-malformed-token"].startswith("Token rejected")
        async with await psycopg.AsyncConnection.connect(checkpoint_db) as conn:
            row = await (
                await conn.execute(
                    "SELECT config FROM public.projects WHERE id = %s", (project_id,)
                )
            ).fetchone()
        encrypted = row[0]["secrets"]
        assert decrypt_dict(encrypted) == dict(zip(secret_keys, CANARIES[1:], strict=True))
        assert_no_secrets({"project": [{"config": row[0]}]})
        assert_no_secrets(await stored_rows(checkpoint_db))
    finally:
        await graph.checkpointer.conn.close()
        await api.delete_raw(f"projects/{project_id}", headers=headers)
        await api.close()
        init_po_clients(None, None)


@pytest.mark.parametrize("value,query", [(1, 1.0), ([[1]], [1]), ([True], [1])])
async def test_metadata_filters_keep_native_postgres_semantics(checkpoint_db, value, query):
    from langgraph.checkpoint.base import empty_checkpoint
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

    from src.agents.po.checkpoint_upgrade import upgrade
    from src.agents.po.graph import _create_postgres_checkpointer

    config = {"configurable": {"thread_id": "filter-thread", "checkpoint_ns": ""}}
    filter = {"echo": query}
    async with AsyncPostgresSaver.from_conn_string(checkpoint_db) as legacy:
        await legacy.setup()
        await legacy.aput(
            config, empty_checkpoint(), {"source": "input", "step": -1, "echo": value}, {}
        )
        expected = [c async for c in legacy.alist(config, filter=filter, limit=1)]
    await asyncio.to_thread(upgrade, checkpoint_db, writers_quiesced=True, apply=True)
    saver = await _create_postgres_checkpointer(checkpoint_db)
    try:
        actual = [c async for c in saver.alist(config, filter=filter, limit=1)]
        assert actual == expected
    finally:
        await saver.conn.close()


async def test_pre_v4_pending_sends_keep_parent_and_order_after_upgrade(checkpoint_db):
    from langgraph.checkpoint.base import empty_checkpoint
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
    from langgraph.checkpoint.serde.types import TASKS
    from langgraph.types import Send

    from src.agents.po.checkpoint_upgrade import upgrade
    from src.agents.po.graph import _create_postgres_checkpointer

    config = {"configurable": {"thread_id": "pending-send-thread", "checkpoint_ns": ""}}
    async with AsyncPostgresSaver.from_conn_string(checkpoint_db) as legacy:
        await legacy.setup()
        parent = await legacy.aput(config, empty_checkpoint(), {"source": "input", "step": -1}, {})
        for index in (0, 1):
            await legacy.aput_writes(
                parent,
                [(TASKS, Send("tools", {"value": SECRET_TEXT, "index": index}))],
                f"task-{index}",
                task_path=f"path-{index}",
            )
        child = empty_checkpoint()
        child["channel_values"] = {"messages": [HumanMessage(content=SECRET_TEXT)]}
        child["channel_versions"] = {"messages": "1"}
        await legacy.aput(parent, child, {"source": "loop", "step": 0}, child["channel_versions"])
        before = await legacy.aget_tuple(config)
        assert [s.arg["index"] for s in before.checkpoint["channel_values"][TASKS]] == [0, 1]
    await asyncio.to_thread(upgrade, checkpoint_db, writers_quiesced=True, apply=True)
    saver = await _create_postgres_checkpointer(checkpoint_db)
    try:
        assert await saver.aget_tuple(config) == before
        assert_no_secrets(await stored_rows(checkpoint_db))
    finally:
        await saver.conn.close()
