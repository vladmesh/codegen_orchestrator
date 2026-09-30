"""Cutting a text for the user into Telegram messages Telegram accepts.

Telegram refuses a message over 4096 characters ("Message is too long"). A text
is first split on ``MESSAGE_BREAK``, where its producer wants a new message; any
part still over ``SAFE_MESSAGE_LENGTH`` is cut at the last paragraph boundary
that fits, else the last line boundary, else the last word boundary, else at the
last point that fits.

The text is Telegram HTML, and every chunk stays well-formed on its own: a cut
never falls inside a tag or an entity, and tags open at a cut are closed at the
end of the chunk and reopened, attributes included, at the start of the next.

Length is counted in UTF-16 code units of the HTML source, tags and entities
included. Telegram counts the text left after parsing the markup, which is never
longer, so a chunk that fits here fits Telegram as HTML and as plain text.
"""

from __future__ import annotations

from dataclasses import dataclass
import re

from shared.contracts.queues.po import MESSAGE_BREAK

TELEGRAM_MESSAGE_LIMIT = 4096
# Headroom under Telegram's limit, so a counting difference cannot push a chunk over.
SAFE_MESSAGE_LENGTH = 4000

_TAG = re.compile(r"<(/?)([a-zA-Z][a-zA-Z0-9-]*)(?:\s[^<>]*)?>")
_ENTITY = re.compile(r"&(?:#[0-9]{1,7}|#[xX][0-9a-fA-F]{1,6}|[a-zA-Z][a-zA-Z0-9]{1,31});")
# A "tag" this long is not markup a chunk could carry; its characters are text.
_MAX_TAG_LENGTH = 1024

# Boundary kinds, best first.
_PARAGRAPH, _LINE, _WORD, _ANY = range(4)


@dataclass(frozen=True)
class _Token:
    text: str
    units: int
    # Lower-cased tag name for an opening or closing tag, else "".
    opens: str = ""
    closes: str = ""

    @property
    def is_markup(self) -> bool:
        return bool(self.opens or self.closes)


@dataclass(frozen=True)
class _Cut:
    end: int  # tokens[start:end] go into the chunk
    resume: int  # the next chunk starts at tokens[resume]
    open_tags: tuple[_Token, ...]  # tags open at ``end``, outermost first


def utf16_length(text: str) -> int:
    """Length as Telegram counts it: UTF-16 code units."""
    return len(text.encode("utf-16-le")) // 2


def split_telegram_text(text: str, limit: int = SAFE_MESSAGE_LENGTH) -> list[str]:
    """The Telegram messages *text* is sent as, in order; empty parts are dropped."""
    chunks: list[str] = []
    for part in text.split(MESSAGE_BREAK):
        part = part.strip()
        if part:
            chunks.extend(_split_part(part, limit))
    return chunks


def _tokenize(text: str) -> list[_Token]:
    tokens: list[_Token] = []
    i = 0
    while i < len(text):
        char = text[i]
        match = None
        if char == "<":
            match = _TAG.match(text, i)
            if match and len(match.group(0)) > _MAX_TAG_LENGTH:
                match = None
            if match:
                name = match.group(2).lower()
                closing = match.group(1) == "/"
                tokens.append(
                    _Token(
                        match.group(0),
                        utf16_length(match.group(0)),
                        opens="" if closing else name,
                        closes=name if closing else "",
                    )
                )
        elif char == "&":
            match = _ENTITY.match(text, i)
            if match:
                tokens.append(_Token(match.group(0), utf16_length(match.group(0))))
        if match:
            i = match.end()
        else:
            tokens.append(_Token(char, utf16_length(char)))
            i += 1
    return tokens


def _closers(open_tags: tuple[_Token, ...] | list[_Token]) -> str:
    return "".join(f"</{tag.opens}>" for tag in reversed(open_tags))


def _apply(stack: list[_Token], token: _Token) -> None:
    if token.opens:
        stack.append(token)
    elif token.closes:
        names = [tag.opens for tag in stack]
        if token.closes in names:
            # Close the innermost tag of that name and whatever is nested in it.
            del stack[len(names) - 1 - names[::-1].index(token.closes) :]


def _boundary(tokens: list[_Token], i: int) -> tuple[int, int] | None:
    """The boundary kind before ``tokens[i]`` and where text resumes after it."""
    text = tokens[i].text
    previous = tokens[i - 1].text
    if text == "\n" and previous != "\n":
        run = i
        while run < len(tokens) and tokens[run].text == "\n":
            run += 1
        return (_PARAGRAPH if run - i > 1 else _LINE), run
    if text in (" ", "\t") and previous not in (" ", "\t", "\n"):
        run = i
        while run < len(tokens) and tokens[run].text in (" ", "\t"):
            run += 1
        return _WORD, run
    return None


def _find_cut(
    tokens: list[_Token], start: int, carried: tuple[_Token, ...], limit: int
) -> _Cut | None:
    """The best cut after ``tokens[start]``, or None when the rest fits whole."""
    stack = list(carried)
    # Units the chunk would have if cut here: the reopened tags, the tokens so
    # far and the closers for what is open. It never decreases along the text,
    # so the scan stops at the first point that does not fit.
    used = sum(tag.units for tag in carried)
    best: dict[int, _Cut] = {}
    for i in range(start, len(tokens)):
        if i > start:
            if used + utf16_length(_closers(stack)) > limit:
                break
            here = _Cut(end=i, resume=i, open_tags=tuple(stack))
            best[_ANY] = here
            boundary = _boundary(tokens, i)
            if boundary is not None:
                kind, resume = boundary
                best[kind] = _Cut(end=i, resume=resume, open_tags=here.open_tags)
        used += tokens[i].units
        _apply(stack, tokens[i])
    else:
        if used + utf16_length(_closers(stack)) <= limit:
            return None
    for kind in (_PARAGRAPH, _LINE, _WORD, _ANY):
        if kind in best:
            return best[kind]
    # Not even one token fits after the reopened tags: take it anyway, so the
    # text always moves forward.
    stack = list(carried)
    _apply(stack, tokens[start])
    return _Cut(end=start + 1, resume=start + 1, open_tags=tuple(stack))


def _split_part(part: str, limit: int) -> list[str]:
    tokens = _tokenize(part)
    chunks: list[str] = []
    start = 0
    carried: tuple[_Token, ...] = ()
    while start < len(tokens):
        cut = _find_cut(tokens, start, carried, limit)
        body = tokens[start:] if cut is None else tokens[start : cut.end]
        if any(not token.is_markup and not token.text.isspace() for token in body):
            opened = "".join(tag.text for tag in carried)
            closed = "" if cut is None else _closers(cut.open_tags)
            chunks.append(opened + "".join(token.text for token in body) + closed)
        if cut is None:
            break
        start, carried = cut.resume, cut.open_tags
    return chunks
