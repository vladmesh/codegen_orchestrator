"""Admin notifications for provisioner results.

Listens to provisioner:results stream and notifies admins about server status.
"""

import asyncio
import json

from redis.asyncio import Redis
import structlog
from telegram import Bot

from shared.contracts.queues.provisioner import ProvisionerResult

logger = structlog.get_logger()

STREAM_KEY = "provisioner:results"
CONSUMER_GROUP = "telegram-bot"
CONSUMER_NAME = "notifier"


class ProvisionerNotifier:
    """Notifies admins about provisioner results."""

    def __init__(self, redis: Redis, admin_ids: set[int]):
        self.redis = redis
        self.admin_ids = admin_ids
        self._running = False

    async def start(self, bot: Bot) -> asyncio.Task:
        """Start the notifier background task."""
        self._running = True
        return asyncio.create_task(self._listen_loop(bot))

    async def stop(self) -> None:
        """Stop the notifier."""
        self._running = False

    async def _ensure_consumer_group(self) -> None:
        """Create consumer group if not exists."""
        try:
            await self.redis.xgroup_create(
                STREAM_KEY,
                CONSUMER_GROUP,
                id="0",
                mkstream=True,
            )
        except Exception as e:
            # Group already exists
            if "BUSYGROUP" not in str(e):
                raise

    async def _listen_loop(self, bot: Bot) -> None:
        """Main loop: listen for provisioner results."""
        await self._ensure_consumer_group()

        logger.info("provisioner_notifier_started", admin_count=len(self.admin_ids))

        while self._running:
            try:
                await self._process_messages(bot)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("provisioner_notifier_error", error=str(e))
                await asyncio.sleep(1)

        logger.info("provisioner_notifier_stopped")

    async def _listen_once(self, bot: Bot) -> None:
        """Process one batch of messages (for testing)."""
        await self._ensure_consumer_group()
        await self._process_messages(bot, block_ms=1000)

    async def _process_messages(self, bot: Bot, block_ms: int = 2000) -> None:
        """Read and process messages from stream."""
        messages = await self.redis.xreadgroup(
            groupname=CONSUMER_GROUP,
            consumername=CONSUMER_NAME,
            streams={STREAM_KEY: ">"},
            count=10,
            block=block_ms,
        )

        if not messages:
            return

        for _stream_name, stream_messages in messages:
            for msg_id, msg_data in stream_messages:
                try:
                    await self._handle_message(bot, msg_id, msg_data)
                    await self.redis.xack(STREAM_KEY, CONSUMER_GROUP, msg_id)
                except Exception as e:
                    logger.error(
                        "provisioner_message_error",
                        msg_id=msg_id,
                        error=str(e),
                    )

    async def _handle_message(self, bot: Bot, msg_id: str, msg_data: dict) -> None:
        """Handle single provisioner result message."""
        payload = json.loads(msg_data.get("data", "{}"))
        result = ProvisionerResult.model_validate(payload)

        logger.info(
            "provisioner_result_received",
            server_handle=result.server_handle,
            status=result.status,
        )

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
        if result.status == "success":
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
