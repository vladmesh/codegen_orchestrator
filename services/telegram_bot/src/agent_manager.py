"""Agent Manager for Telegram Bot.

Manages the mapping between Telegram users and their Worker containers.
Responsible for ensuring a valid container exists for a user and handling
session persistence.
"""

import json

import redis.asyncio as redis
import structlog

from src.clients.workers_spawner import workers_spawner
from src.config import get_settings

logger = structlog.get_logger(__name__)

# Redis key prefix for user->agent mapping
USER_AGENT_KEY_PREFIX = "telegram:user_agent:"


class AgentManager:
    """Manages user agent sessions."""

    def __init__(self) -> None:
        settings = get_settings()
        self.redis = redis.from_url(settings.redis_url, decode_responses=True)

    async def close(self) -> None:
        """Close resources."""
        await self.redis.aclose()

    async def get_or_create_agent(self, user_id: int) -> str:
        """Get existing valid agent ID or create a new one.

        Args:
            user_id: Telegram user ID

        Returns:
            agent_id: Valid active agent ID
        """
        key = f"{USER_AGENT_KEY_PREFIX}{user_id}"

        # 1. Check if we have a mapped agent
        agent_id = await self.redis.get(key)

        if agent_id:
            # 2. Check if it's still alive/valid
            try:
                status = await workers_spawner.get_status(agent_id)
                if status and status.get("state") != "deleted":
                    # Valid agent found
                    logger.info("found_existing_agent", user_id=user_id, agent_id=agent_id)
                    return agent_id

                logger.info(
                    "existing_agent_invalid", user_id=user_id, agent_id=agent_id, status=status
                )
            except Exception as e:
                logger.warning("agent_status_check_failed", agent_id=agent_id, error=str(e))
                # Fall through to create new one

        # 3. Create new agent
        # For MVP/Dev: Always mount session volume to save cost/context
        # In production this might be conditional based on user tier
        mount_volume = True

        logger.info("creating_new_agent", user_id=user_id, mount_volume=mount_volume)

        try:
            agent_id = await workers_spawner.create_agent(
                str(user_id), mount_session_volume=mount_volume
            )

            # Save mapping (assume worker TTL matches logical expiry)
            # Worker default TTL is 2 hours. We keep mapping for longer?
            # If worker deletes itself, status check above will catch it.
            await self.redis.set(key, agent_id)

            logger.info("new_agent_created", user_id=user_id, agent_id=agent_id)
            return agent_id

        except Exception as e:
            logger.error("agent_creation_failed", user_id=user_id, error=str(e))
            raise

    async def send_message(self, user_id: int, message: str) -> None:
        """Send a message to the user's agent.

        Args:
            user_id: Telegram user ID
            message: Text message
        """
        agent_id = await self.get_or_create_agent(user_id)

        # Use orchestrator response tool command
        # This is what the agent receives.
        # Wait, the spawner sends the command TO the agent.
        # If we send "Hi", we want the agent to process it.
        # But `cli-agent.send_command` executes a SHELL command in the container.
        # If we want to talk to Claude, we run: `claude -p "message"`
        # So we should construct the CLI command here!

        # NOTE: Using --resume logic inside the container?
        # The universal worker has `orchestrator-cli`.
        # Logic:
        # We want to run: 'claude -p "message"'
        # BUT we need to handle session persistence per user inside the container too?
        # If we mount volume, ~/.claude is preserved.
        # So providing NO session ID to `claude` might default to last session?
        # No, Claude CLI usually needs explicit session management or it creates new one.
        # But if we mount `.claude`, maybe we don't need `--resume` if we just want "a session"?
        # Actually, original agent logic used `--resume {session_id}`.
        # `workers-spawner` manages containers. The container persists.
        # So we can just keep using the SAME container.
        # Does executing `claude` repeatedly in the SAME container maintain context?
        # NO. `claude` CLI is a process that runs and exits.
        # Unless we use `claude` in REPL mode via `expect`? No.
        # We run `claude -p "..."`.
        # To maintain context, we MUST use `--resume {session_id}`.
        # Where do we get `session_id`?
        # The container is persistent now (for 2 hours).
        # We can store `session_id` in Redis `telegram:user_session:{user_id}`.
        # OR we can assume `claude` stores it in the mounted volume if we use it?
        # Claude CLI outputs `session_id` in JSON.

        # So:
        # 1. Get current session_id from Redis.
        # 2. Run `claude -p "msg" --resume {session_id}`
        # 3. Parse output, update session_id.

        # Wait, if we use `mount_session_volume`, all sessions are in the volume.
        # We still need the ID to pick the right one.

        session_key = f"telegram:user_session_id:{user_id}"
        session_id = await self.redis.get(session_key)

        # Use single quotes for shell safety - escape any single quotes in message
        # Single quote escaping: replace ' with '\''  (end quote, escaped quote, start quote)
        safe_message = message.replace("'", "'\\''")

        cmd_parts = [
            "claude",
            "--dangerously-skip-permissions",
            "-p",
            f"'{safe_message}'",
            "--output-format",
            "json",
        ]

        # If we have a session ID, try to resume it
        if session_id:
            cmd_parts.extend(["--resume", session_id])

        full_command = " ".join(cmd_parts)

        logger.info(
            "sending_agent_command",
            user_id=user_id,
            agent_id=agent_id,
            has_session=bool(session_id),
        )

        # Execute (long timeout for LLM)
        try:
            result = await workers_spawner.send_command(agent_id, full_command, timeout=120)
        except Exception as e:
            # If command fails (e.g. session not found), we might want to retry without session?
            # Claude CLI usually handles invalid session by erroring?
            # For now, propagate error
            raise e

        # Parse output
        output = result.get("output", "")
        # Output is from stdout.
        # We expect JSON: { "result": "...", "session_id": "..." }

        try:
            data = json.loads(output)
            response_text = data.get("result", "")
            new_session_id = data.get("session_id")

            if new_session_id:
                await self.redis.set(session_key, new_session_id)

            # We need to send this response BACK to the user!
            # The `main.py` outgoing consumer listened to `agent:outgoing` in legacy.
            # Here we are in direct control. We should return the text or send it?
            # `AgentManager.send_message` serves `handle_message`.
            # If we return it, `handle_message` can reply.

            return response_text

        except json.JSONDecodeError:
            logger.warning("invalid_agent_json_output", output=output)
            # Fallback
            return output


# Singleton
agent_manager = AgentManager()
