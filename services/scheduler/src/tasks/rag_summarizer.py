"""RAG conversation summarizer worker."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
import os

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
import structlog
import tiktoken

from shared.models import RAGConversationSummary, RAGMessage, User
from src.db import async_session_maker

logger = structlog.get_logger()

ENCODING_NAME = "cl100k_base"
SUMMARY_POLL_INTERVAL = 30
SUMMARY_TEMPERATURE = 0.2
SUMMARY_MAX_TOKENS = 400


@dataclass(frozen=True)
class SummaryConfig:
    provider: str
    model: str
    api_key: str
    token_threshold: int

    @property
    def base_url(self) -> str:
        if self.provider == "openrouter":
            return "https://openrouter.ai/api/v1"
        return "https://api.openai.com/v1"


async def rag_summarizer_worker() -> None:
    """Background worker to summarize raw chat messages."""
    logger.info("rag_summarizer_worker_started")
    encoding = tiktoken.get_encoding(ENCODING_NAME)
    last_config_error: str | None = None

    while True:
        config = _load_config()
        if not config:
            if last_config_error != "missing_config":
                logger.warning("rag_summarizer_disabled", reason="missing_config")
                last_config_error = "missing_config"
            await asyncio.sleep(SUMMARY_POLL_INTERVAL)
            continue

        last_config_error = None

        try:
            async with async_session_maker() as db:
                await _summarize_pending(db, config, encoding)
        except Exception as exc:
            logger.error(
                "rag_summarizer_failed",
                error=str(exc),
                error_type=type(exc).__name__,
                exc_info=True,
            )

        await asyncio.sleep(SUMMARY_POLL_INTERVAL)


def _load_config() -> SummaryConfig | None:
    provider = os.getenv("RAG_SUMMARY_PROVIDER")
    model = os.getenv("RAG_SUMMARY_MODEL")
    threshold_raw = os.getenv("RAG_SUMMARY_TOKEN_THRESHOLD")

    if not provider or not model or not threshold_raw:
        return None

    try:
        token_threshold = int(threshold_raw)
    except ValueError:
        logger.warning("rag_summarizer_invalid_threshold", value=threshold_raw)
        return None

    if provider == "openai":
        api_key = os.getenv("OPENAI_API_KEY")
    elif provider == "openrouter":
        api_key = os.getenv("OPEN_ROUTER_KEY")
    else:
        logger.warning("rag_summarizer_invalid_provider", provider=provider)
        return None

    if not api_key:
        return None

    return SummaryConfig(
        provider=provider,
        model=model,
        api_key=api_key,
        token_threshold=token_threshold,
    )


async def _summarize_pending(
    db: AsyncSession,
    config: SummaryConfig,
    encoding: tiktoken.Encoding,
) -> None:
    result = await db.execute(
        select(RAGMessage)
        .where(RAGMessage.summarized_at.is_(None))
        .order_by(RAGMessage.user_id, RAGMessage.created_at)
    )
    messages = result.scalars().all()
    if not messages:
        return

    grouped: dict[int, list[RAGMessage]] = defaultdict(list)
    for message in messages:
        grouped[message.user_id].append(message)

    for user_id, user_messages in grouped.items():
        selected = _select_messages(user_messages, config.token_threshold, encoding)
        if not selected:
            continue

        summary_text = await _generate_summary(config, selected)
        if not summary_text:
            continue

        project_id = _resolve_project_id(selected)
        thread_id = await _resolve_thread_id(db, user_id)
        summary = RAGConversationSummary(
            user_id=user_id,
            project_id=project_id,
            thread_id=thread_id,
            summary_text=summary_text,
            message_ids=[str(message.id) for message in selected],
        )
        db.add(summary)

        summarized_at = datetime.now(UTC)
        for message in selected:
            message.summarized_at = summarized_at

        await db.commit()


def _select_messages(
    messages: list[RAGMessage],
    token_threshold: int,
    encoding: tiktoken.Encoding,
) -> list[RAGMessage]:
    total_tokens = 0
    selected: list[RAGMessage] = []

    for message in messages:
        if not message.message_text:
            continue
        tokens = len(encoding.encode(f"{message.role}: {message.message_text}"))
        selected.append(message)
        total_tokens += tokens
        if total_tokens >= token_threshold:
            break

    if total_tokens < token_threshold:
        return []

    return selected


async def _generate_summary(config: SummaryConfig, messages: list[RAGMessage]) -> str | None:
    conversation = "\n".join(
        f"{msg.role}: {msg.message_text.strip()}" for msg in messages if msg.message_text
    )
    if not conversation:
        return None

    system_prompt = (
        "You are a summarization assistant for project conversations. "
        "Summarize the exchange into concise bullet points with decisions, "
        "requirements, constraints, and open questions."
    )
    user_prompt = f"Summarize this conversation:\n\n{conversation}"

    payload = {
        "model": config.model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": SUMMARY_TEMPERATURE,
        "max_tokens": SUMMARY_MAX_TOKENS,
    }

    headers = {"Authorization": f"Bearer {config.api_key}"}

    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.post(
                f"{config.base_url}/chat/completions",
                json=payload,
                headers=headers,
            )
            response.raise_for_status()
    except httpx.HTTPError as exc:
        logger.warning("rag_summarizer_request_failed", error=str(exc))
        return None

    data = response.json()
    try:
        return data["choices"][0]["message"]["content"].strip()
    except (KeyError, IndexError, AttributeError):
        logger.warning("rag_summarizer_response_invalid")
        return None


def _resolve_project_id(messages: list[RAGMessage]) -> str | None:
    project_ids = {message.project_id for message in messages if message.project_id}
    if len(project_ids) == 1:
        return next(iter(project_ids))
    return None


async def _resolve_thread_id(db: AsyncSession, user_id: int) -> str:
    user = await db.get(User, user_id)
    if user and user.telegram_id:
        return f"user_{user.telegram_id}"
    return f"user_{user_id}"
