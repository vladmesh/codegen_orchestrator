"""Admin notifications for provisioner results.

Listens to provisioner:results stream and notifies admins about server status.
"""

import asyncio

import structlog
from telegram import Bot

from shared.contracts.queues.provisioner import ProvisionerResult
from shared.contracts.vocab import ResultStatus
from shared.queues import PROVISIONER_RESULTS, TELEGRAM_BOT_GROUP
from shared.redis_client import RedisStreamClient

logger = structlog.get_logger()

CONSUMER_NAME = "notifier"


class ProvisionerNotifier:
    """Notifies admins about provisioner results."""

    def __init__(self, client: RedisStreamClient, admin_ids: set[int]):
        self.client = client
        self.admin_ids = admin_ids
        self._running = False

    async def start(self, bot: Bot) -> asyncio.Task:
        """Start the notifier background task."""
        self._running = True
        return asyncio.create_task(self._listen_loop(bot))

    async def stop(self) -> None:
        """Stop the notifier."""
        self._running = False

    async def _listen_loop(self, bot: Bot) -> None:
        """Main loop: listen for provisioner results."""
        logger.info("provisioner_notifier_started", admin_count=len(self.admin_ids))

        try:
            async for msg in self.client.consume(
                PROVISIONER_RESULTS,
                TELEGRAM_BOT_GROUP,
                CONSUMER_NAME,
                auto_ack=True,
            ):
                if not self._running:
                    break
                if msg is None:
                    continue
                try:
                    await self._handle_message(bot, msg.message_id, msg.data)
                except Exception as e:
                    logger.error(
                        "provisioner_message_error",
                        msg_id=msg.message_id,
                        error=str(e),
                    )
        except asyncio.CancelledError:
            pass

        logger.info("provisioner_notifier_stopped")

    async def _handle_message(self, bot: Bot, msg_id: str, data: dict) -> None:
        """Handle single provisioner result message."""
        result = ProvisionerResult.model_validate(data)

        logger.info(
            "provisioner_result_received",
            server_handle=result.server_handle,
            status=result.status,
        )

        if result.status == ResultStatus.SUPERSEDED:
            # Stale completion superseded by a newer attempt for the same server.
            # It is a no-op, not a failure, so do not notify admins.
            logger.info("provisioner_result_superseded_ignored", server_handle=result.server_handle)
            return

        if not self.admin_ids:
            logger.debug("no_admins_to_notify")
            return

        text = self._format_result(result)

        for admin_id in self.admin_ids:
            try:
                await bot.send_message(chat_id=admin_id, text=text)
            except Exception as e:
                logger.warning(
                    "admin_notification_failed",
                    admin_id=admin_id,
                    error=str(e),
                )

    def _format_result(self, result: ProvisionerResult) -> str:
        """Format provisioner result for Telegram message."""
        if result.status == ResultStatus.SUCCESS:
            lines = [
                "✅ Provisioning завершён",
                f"Сервер: {result.server_handle}",
            ]
            if result.server_ip:
                lines.append(f"IP: {result.server_ip}")
            if result.services_redeployed:
                lines.append(f"Редеплой сервисов: {result.services_redeployed}")
        else:
            lines = [
                "❌ Provisioning failed",
                f"Сервер: {result.server_handle}",
            ]
            if result.errors:
                lines.append("Ошибки:")
                for err in result.errors[:3]:  # Max 3 errors
                    lines.append(f"  • {err}")

        return "\n".join(lines)
