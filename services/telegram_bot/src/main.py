"""Telegram Bot - Main entry point."""

import asyncio
import os

from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters


async def start(update: Update, context) -> None:
    """Handle /start command."""
    await update.message.reply_text(
        "Привет! Я оркестратор для генерации проектов.\n"
        "Опиши, какой проект ты хочешь создать."
    )


async def handle_message(update: Update, context) -> None:
    """Handle incoming messages."""
    user_message = update.message.text
    user_id = update.effective_user.id

    # TODO: Send to LangGraph orchestrator
    langgraph_url = os.getenv("LANGGRAPH_URL")

    await update.message.reply_text(
        f"Получил: {user_message}\n"
        f"(LangGraph: {langgraph_url})"
    )


def main() -> None:
    """Run the bot."""
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")

    app = Application.builder().token(token).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    print("Bot started")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
