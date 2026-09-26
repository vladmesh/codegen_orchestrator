"""Every text for the user leaves the bot through ``send_text``.

The canary of 2026-09-25 showed a user ``Ошибка: Message is too long``: PO's reply
was over Telegram's limit and the raw error came back as the answer. These tests
hold what replaced it: the text is split on ``MESSAGE_BREAK`` and under the limit,
every chunk is well-formed HTML, the reply and proactive paths both use it, a
proactive retry resumes at the failed chunk, and no exception text reaches a chat.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from html.parser import HTMLParser
import importlib.util
from pathlib import Path
import re
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from shared.contracts.queues.po import MESSAGE_BREAK, POProactiveMessage
from shared.telegram_text import SAFE_MESSAGE_LENGTH, TELEGRAM_MESSAGE_LIMIT, utf16_length
import src.main as bot_main
from src.proactive import SendProgress, attempt_proactive_delivery, send_text

LIMIT = SAFE_MESSAGE_LENGTH
CHAT_ID = 4242


@dataclass
class _Sent:
    text: str
    parse_mode: str | None


@dataclass
class StubBot:
    """Records what Telegram accepted; refuses the calls ``refuse`` says to."""

    accepted: list[_Sent] = field(default_factory=list)
    calls: list[_Sent] = field(default_factory=list)
    # Called with each attempted send; returning True makes that call fail.
    refuse: object = None

    async def send_message(self, chat_id: int, text: str, parse_mode: str | None = None) -> None:
        assert chat_id == CHAT_ID
        sent = _Sent(text, parse_mode)
        self.calls.append(sent)
        if self.refuse is not None and self.refuse(sent, len(self.calls)):
            raise RuntimeError("Bad Request: can't parse entities")
        self.accepted.append(sent)

    @property
    def texts(self) -> list[str]:
        return [sent.text for sent in self.accepted]


class _Balance(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.stack: list[str] = []

    def handle_starttag(self, tag, attrs):
        self.stack.append(tag)

    def handle_endtag(self, tag):
        assert self.stack and self.stack[-1] == tag, f"</{tag}> closes {self.stack}"
        self.stack.pop()


def _assert_well_formed(chunk: str) -> None:
    parser = _Balance()
    parser.feed(chunk)
    parser.close()
    assert parser.stack == [], f"left open: {parser.stack}"
    # An ampersand always starts a whole entity: none was cut in half.
    for match in re.finditer("&", chunk):
        assert re.match(r"&(#\d+|#x[0-9a-fA-F]+|[a-zA-Z]+);", chunk[match.start() :]), chunk


def _words(count: int, word: str = "word") -> str:
    return " ".join([word] * count)


PARAGRAPHS = [f"Paragraph {i:03d}." for i in range(300)]
# 16 units per paragraph with its separator: 250 of them fit in 4000.
BOLD_TEXT = "<b>" + _words(1000, "bold") + "</b>"
LINK_TEXT = 'See <a href="https://example.org/x?a=1&amp;b=2">' + _words(1000, "link") + "</a> end"


def _check_bold(chunks: list[str]) -> None:
    assert len(chunks) == 2
    assert all(chunk.startswith("<b>") and chunk.endswith("</b>") for chunk in chunks)
    inner = " ".join(chunk.removeprefix("<b>").removesuffix("</b>") for chunk in chunks)
    assert inner == _words(1000, "bold")


def _check_link(chunks: list[str]) -> None:
    assert len(chunks) == 2
    opening = '<a href="https://example.org/x?a=1&amp;b=2">'
    assert chunks[0].startswith("See " + opening) and chunks[0].endswith("</a>")
    assert chunks[1].startswith(opening) and chunks[1].endswith("</a> end")


# (id, text, the chunks Telegram must receive in order, or a check over them)
CASES = [
    ("exact_limit", "a" * LIMIT, ["a" * LIMIT]),
    ("limit_plus_one", "a" * (LIMIT + 1), ["a" * LIMIT, "a"]),
    (
        "one_10k_paragraph_splits_at_words",
        _words(2000),
        [_words(800), _words(800), _words(400)],
    ),
    (
        "many_short_paragraphs_split_at_paragraphs",
        "\n\n".join(PARAGRAPHS),
        ["\n\n".join(PARAGRAPHS[:250]), "\n\n".join(PARAGRAPHS[250:])],
    ),
    (
        "lines_when_there_is_no_paragraph",
        "\n".join(PARAGRAPHS),
        # 15 units per line with its separator: 266 fit.
        ["\n".join(PARAGRAPHS[:266]), "\n".join(PARAGRAPHS[266:])],
    ),
    (
        "message_break_starts_a_new_message",
        f"Summary.\n\n{MESSAGE_BREAK}\n\nDetails.",
        ["Summary.", "Details."],
    ),
    (
        "empty_part_between_breaks_is_dropped",
        f"one{MESSAGE_BREAK}  \n {MESSAGE_BREAK}{MESSAGE_BREAK}two{MESSAGE_BREAK}",
        ["one", "two"],
    ),
    ("bold_spanning_a_cut_is_closed_and_reopened", BOLD_TEXT, _check_bold),
    ("link_spanning_a_cut_keeps_its_href", LINK_TEXT, _check_link),
    (
        "entity_at_the_cut_moves_whole",
        "a" * (LIMIT - 2) + "&amp;" + "b" * 10,
        ["a" * (LIMIT - 2), "&amp;" + "b" * 10],
    ),
    (
        "tag_at_the_cut_moves_whole",
        "a" * (LIMIT - 2) + "<i>" + "b" * 10 + "</i>",
        ["a" * (LIMIT - 2), "<i>" + "b" * 10 + "</i>"],
    ),
    (
        "astral_characters_count_as_two",
        "😀" * (LIMIT // 2 + 1),
        ["😀" * (LIMIT // 2), "😀"],
    ),
]


@pytest.mark.parametrize(("text", "expected"), [c[1:] for c in CASES], ids=[c[0] for c in CASES])
@pytest.mark.asyncio
async def test_send_text_splits(text, expected):
    bot = StubBot()

    await send_text(bot, CHAT_ID, text)

    chunks = bot.texts
    if callable(expected):
        expected(chunks)
    else:
        assert chunks == expected
    assert all(sent.parse_mode == "HTML" for sent in bot.accepted)
    for chunk in chunks:
        assert MESSAGE_BREAK not in chunk
        assert 0 < utf16_length(chunk) <= LIMIT < TELEGRAM_MESSAGE_LIMIT
        _assert_well_formed(chunk)


@pytest.mark.asyncio
async def test_plain_text_fallback_is_per_chunk():
    bot = StubBot(refuse=lambda sent, _n: sent.parse_mode == "HTML" and sent.text == "two")

    await send_text(bot, CHAT_ID, MESSAGE_BREAK.join(["one", "two", "three"]))

    assert [(s.text, s.parse_mode) for s in bot.accepted] == [
        ("one", "HTML"),
        ("two", None),
        ("three", "HTML"),
    ]


@pytest.mark.asyncio
async def test_progress_counts_the_chunks_accepted_before_a_failure():
    bot = StubBot(refuse=lambda sent, _n: sent.text == "two")
    progress = SendProgress()

    with pytest.raises(RuntimeError):
        await send_text(bot, CHAT_ID, MESSAGE_BREAK.join(["one", "two", "three"]), progress)

    assert progress.sent == 1
    assert bot.texts == ["one"]


def _long_reply() -> str:
    return "\n\n".join(PARAGRAPHS) + MESSAGE_BREAK + _words(2000)


class TestReplyPath:
    @pytest.mark.asyncio
    async def test_reply_goes_through_send_text(self):
        app = MagicMock()
        with patch("src.main.send_text", new=AsyncMock()) as sender:
            await bot_main._send_response_to_user(app, CHAT_ID, "hello")

        sender.assert_awaited_once_with(app.bot, CHAT_ID, "hello")

    @pytest.mark.asyncio
    async def test_long_reply_arrives_in_order_under_the_limit(self):
        app = MagicMock()
        app.bot = StubBot()

        await bot_main._send_response_to_user(app, CHAT_ID, _long_reply())

        chunks = app.bot.texts
        assert len(chunks) == 5
        assert chunks[0].startswith("Paragraph 000.")
        assert chunks[1].endswith("Paragraph 299.")
        assert chunks[2:] == [_words(800), _words(800), _words(400)]
        assert all(utf16_length(chunk) <= LIMIT for chunk in chunks)


def _proactive(text: str) -> POProactiveMessage:
    return POProactiveMessage(text=text, telegram_chat_id=str(CHAT_ID))


class TestProactivePath:
    @pytest.fixture(autouse=True)
    def _no_backoff(self, monkeypatch):
        monkeypatch.setattr("src.proactive.PROACTIVE_RETRY_DELAY_S", 0)

    @pytest.mark.asyncio
    async def test_proactive_goes_through_send_text(self):
        bot = StubBot()
        with patch("src.proactive.send_text", new=AsyncMock()) as sender:
            assert await attempt_proactive_delivery(bot, _proactive("hello")) is None

        sender.assert_awaited_once()
        assert sender.await_args.args[:3] == (bot, CHAT_ID, "hello")

    @pytest.mark.asyncio
    async def test_long_notification_arrives_in_order_under_the_limit(self):
        bot = StubBot()

        assert await attempt_proactive_delivery(bot, _proactive(_long_reply())) is None

        chunks = bot.texts
        assert len(chunks) == 5
        assert chunks[0].startswith("Paragraph 000.")
        assert chunks[2:] == [_words(800), _words(800), _words(400)]
        assert all(utf16_length(chunk) <= LIMIT for chunk in chunks)

    @pytest.mark.asyncio
    async def test_retry_resumes_at_the_failed_chunk(self):
        # The second chunk fails as HTML and as plain text on the first attempt
        # (calls 2 and 3), then goes through on the retry.
        bot = StubBot(refuse=lambda _sent, n: n in (2, 3))
        text = MESSAGE_BREAK.join(["one", "two", "three"])

        assert await attempt_proactive_delivery(bot, _proactive(text)) is None

        assert [s.text for s in bot.calls] == ["one", "two", "two", "two", "three"]
        assert bot.texts == ["one", "two", "three"]


class TestNoRawErrorText:
    @pytest.mark.parametrize(
        "error",
        [RuntimeError("Message is too long"), ValueError("Bad Request: Message is too long")],
        ids=["runtime_error", "any_other_error"],
    )
    @pytest.mark.asyncio
    async def test_failed_message_gets_a_fixed_apology(self, error, monkeypatch):
        monkeypatch.setattr(bot_main, "_stream_client", MagicMock())
        monkeypatch.setattr(bot_main, "_post_rag_message", AsyncMock())
        monkeypatch.setattr(bot_main, "_send_to_po_and_wait", AsyncMock(side_effect=error))
        update = MagicMock()
        update.message.reply_text = AsyncMock()
        update.message.text = "hi"
        context = MagicMock()
        context.user_data = {}

        await bot_main.handle_message(update, context)

        update.message.reply_text.assert_awaited_once_with(bot_main.MESSAGE_FAILED_REPLY)
        assert "too long" not in bot_main.MESSAGE_FAILED_REPLY


def test_delivery_module_loads_by_path():
    """The backend integration suite loads proactive.py by path, outside ``sys.modules``.

    It does so to keep its own ``src`` package apart from the bot's; the module
    has to stay loadable that way, and ``send_text`` has to work from it.
    """
    path = Path(__file__).resolve().parents[2] / "src" / "proactive.py"
    spec = importlib.util.spec_from_file_location("telegram_bot_proactive_by_path", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert module.SendProgress().sent == 0
    assert callable(module.send_text)
