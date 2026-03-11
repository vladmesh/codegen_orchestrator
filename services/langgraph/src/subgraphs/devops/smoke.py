"""Smoke tester node for post-deploy verification.

Runs deterministic health checks after deployment:
- Backend modules: GET /health → HTTP 200
- Telegram bot modules: Telethon /start → non-empty response
"""

import asyncio
import os

import asyncssh
import httpx
import structlog

try:
    from telethon import TelegramClient
except ImportError:
    TelegramClient = None  # type: ignore[assignment, misc]

from ...clients.api import api_client
from ...nodes.base import FunctionalNode, RetryPolicy
from .state import DevOpsState

logger = structlog.get_logger()

HEALTH_CHECK_TIMEOUT = 10
HEALTH_CHECK_RETRIES = 3
HEALTH_CHECK_RETRY_DELAY = 5
HTTP_OK = 200
CONTAINER_LOG_TAIL = 50
SERVICE_BASE_DIR = "/opt/services"


class SmokeTesterNode(FunctionalNode):
    """Run smoke tests against deployed services."""

    def __init__(self):
        super().__init__(
            node_id="smoke_tester",
            retry_policy=RetryPolicy(max_attempts=1),
        )

    async def run(self, state: DevOpsState) -> dict:
        """Run smoke tests for all deployed modules."""
        project_spec = state.get("project_spec") or {}
        config = project_spec.get("config") or {}
        modules = config.get("modules", [])
        allocated_resources = state.get("allocated_resources", {})

        checks = []
        errors = []

        for module in modules:
            resource = self._find_resource(allocated_resources, module)
            if not resource:
                logger.warning("smoke_no_resource", module=module)
                checks.append(
                    {
                        "module": module,
                        "result": "skip",
                        "detail": "No allocated resource found",
                    }
                )
                continue

            server_ip = resource["server_ip"]
            port = resource["port"]

            if module == "backend":
                check = await self._check_backend_health(server_ip, port)
                checks.append(check)
                if check["result"] == "fail":
                    errors.append(f"Smoke failed: backend health check — {check['detail']}")

            elif module == "tg_bot":
                check = await self._check_tg_bot(state, server_ip, port)
                checks.append(check)
                if check["result"] == "fail":
                    errors.append(f"Smoke failed: tg_bot check — {check['detail']}")

            else:
                logger.info("smoke_skip_unknown_module", module=module)
                checks.append(
                    {
                        "module": module,
                        "result": "skip",
                        "detail": f"No smoke check for module type: {module}",
                    }
                )

        overall = "fail" if any(c["result"] == "fail" for c in checks) else "pass"

        # Enrich failed checks with container logs from the server
        if overall == "fail":
            project_name = (state.get("project_spec") or {}).get("name")
            # Pick server_handle from the first resource that has one
            server_handle = None
            first_server_ip = None
            for alloc in allocated_resources.values():
                if isinstance(alloc, dict) and alloc.get("server_handle"):
                    server_handle = alloc["server_handle"]
                    first_server_ip = alloc.get("server_ip")
                    break

            if project_name and server_handle and first_server_ip:
                container_logs = await self._fetch_container_logs(
                    first_server_ip, server_handle, project_name
                )
                if container_logs:
                    for check in checks:
                        if check["result"] == "fail":
                            check["detail"] += f"\n\nContainer logs:\n{container_logs}"

        logger.info(
            "smoke_complete",
            status=overall,
            checks_count=len(checks),
            failed=[c["module"] for c in checks if c["result"] == "fail"],
        )

        result = {
            "smoke_result": {"status": overall, "checks": checks},
        }
        if errors:
            result["errors"] = errors
        return result

    def _find_resource(self, allocated_resources: dict, module: str) -> dict | None:
        """Find allocated resource entry for a given module."""
        for alloc in allocated_resources.values():
            if isinstance(alloc, dict) and alloc.get("service_name") == module:
                return alloc
        return None

    async def _fetch_container_logs(
        self,
        server_ip: str,
        server_handle: str,
        project_name: str,
    ) -> str | None:
        """SSH into server and fetch docker compose logs for the project.

        Returns log output (truncated) or None if fetch fails.
        """
        try:
            ssh_key = await api_client.get_server_ssh_key(server_handle)
            if not ssh_key:
                logger.warning("smoke_logs_no_ssh_key", server_handle=server_handle)
                return None

            key = asyncssh.import_private_key(ssh_key)
            service_dir = f"{SERVICE_BASE_DIR}/{project_name}"
            cmd = (
                f"cd {service_dir} && "
                f"docker compose -f infra/compose.base.yml -f infra/compose.prod.yml "
                f"logs --tail={CONTAINER_LOG_TAIL} --no-color 2>&1"
            )

            async with asyncssh.connect(
                server_ip,
                username="root",
                known_hosts=None,
                client_keys=[key],
            ) as conn:
                result = await conn.run(cmd, check=False)
                return result.stdout.strip() if result.stdout else None

        except Exception:
            logger.warning("smoke_logs_fetch_failed", server_ip=server_ip, exc_info=True)
            return None

    async def _check_backend_health(self, server_ip: str, port: int) -> dict:
        """GET /health with retries."""
        url = f"http://{server_ip}:{port}/health"
        last_error = None

        async with httpx.AsyncClient() as client:
            for attempt in range(HEALTH_CHECK_RETRIES):
                try:
                    response = await client.get(url, timeout=HEALTH_CHECK_TIMEOUT)
                    if response.status_code == HTTP_OK:
                        return {
                            "module": "backend",
                            "result": "pass",
                            "detail": f"HTTP {response.status_code}",
                        }
                    last_error = f"HTTP {response.status_code}"
                except (httpx.ConnectTimeout, httpx.ConnectError, httpx.ReadTimeout) as e:
                    last_error = str(e)

                if attempt < HEALTH_CHECK_RETRIES - 1:
                    logger.info(
                        "smoke_backend_retry",
                        attempt=attempt + 1,
                        url=url,
                        error=last_error,
                    )
                    await asyncio.sleep(HEALTH_CHECK_RETRY_DELAY)

        return {
            "module": "backend",
            "result": "fail",
            "detail": last_error or "Unknown error",
        }

    async def _check_tg_bot(self, state: DevOpsState, server_ip: str, port: int) -> dict:
        """Send /start to the bot via Telethon, verify non-empty response."""
        # Check env vars — graceful skip if not configured
        api_id = os.getenv("TELETHON_API_ID")
        api_hash = os.getenv("TELETHON_API_HASH")
        session_path = os.getenv("TELETHON_SESSION_PATH")

        if not all([api_id, api_hash, session_path]):
            logger.warning("smoke_tg_bot_skip", reason="Telethon env vars not configured")
            return {
                "module": "tg_bot",
                "result": "skip",
                "detail": "Telethon env vars not configured",
            }

        if TelegramClient is None:
            logger.warning("smoke_tg_bot_skip", reason="telethon not installed")
            return {
                "module": "tg_bot",
                "result": "skip",
                "detail": "telethon package not installed",
            }

        # Get bot username via Bot API getMe
        resolved_secrets = state.get("resolved_secrets", {})
        bot_token = resolved_secrets.get("TELEGRAM_BOT_TOKEN")
        if not bot_token:
            return {
                "module": "tg_bot",
                "result": "skip",
                "detail": "No TELEGRAM_BOT_TOKEN in resolved_secrets",
            }

        try:
            async with httpx.AsyncClient() as http:
                resp = await http.get(
                    f"https://api.telegram.org/bot{bot_token}/getMe",
                    timeout=HEALTH_CHECK_TIMEOUT,
                )
                data = resp.json()
                bot_username = data.get("result", {}).get("username")

            if not bot_username:
                return {
                    "module": "tg_bot",
                    "result": "fail",
                    "detail": "Could not get bot username from getMe",
                }

            # Connect Telethon and send /start
            client = TelegramClient(session_path, int(api_id), api_hash)
            try:
                await client.start()
                await client.send_message(f"@{bot_username}", "/start")
                response = await client.get_response(f"@{bot_username}", timeout=15)

                if response and response.text:
                    return {
                        "module": "tg_bot",
                        "result": "pass",
                        "detail": f"Bot responded: {response.text[:100]}",
                    }
                return {
                    "module": "tg_bot",
                    "result": "fail",
                    "detail": "Bot sent empty response",
                }
            finally:
                await client.disconnect()

        except TimeoutError:
            return {
                "module": "tg_bot",
                "result": "fail",
                "detail": "No response from bot within 15s",
            }
        except Exception as e:
            logger.error("smoke_tg_bot_error", error=str(e), error_type=type(e).__name__)
            return {
                "module": "tg_bot",
                "result": "fail",
                "detail": f"Error: {e}",
            }


smoke_tester_node = SmokeTesterNode()
