"""Smoke tester node for post-deploy verification.

Runs deterministic health checks after deployment:
- Backend modules: GET /health → HTTP 200
- Telegram bot modules: Bot API getMe + the tg_bot container running on the server
"""

import asyncio
import shlex

import asyncssh
import httpx
import structlog

from shared.contracts.dto.project import ServiceModule

from ...clients.api import api_client
from ...nodes.base import FunctionalNode, RetryPolicy
from ...runtime_identity import project_spec_runtime_slug
from .state import DevOpsState

logger = structlog.get_logger()

HEALTH_CHECK_TIMEOUT = 10
HEALTH_CHECK_RETRIES = 3
HEALTH_CHECK_RETRY_DELAY = 5
HTTP_OK = 200
CONTAINER_LOG_TAIL = 50
SERVICE_BASE_DIR = "/opt/services"
BOT_API_BASE = "https://api.telegram.org"
# Compose service names on the deployed project match the module names
TG_BOT_SERVICE = ServiceModule.TG_BOT.value
SMOKE_CHECKED_MODULES = (ServiceModule.BACKEND, ServiceModule.TG_BOT)


class TgBotCheckFailed(Exception):
    """A mandatory tg_bot probe could not confirm the deployed bot."""


def compose_command(project_name: str, subcommand: str) -> str:
    """Build a docker compose command for a deployed project."""
    quoted_name = shlex.quote(project_name)
    infra_dir = shlex.quote(f"{SERVICE_BASE_DIR}/{project_name}/infra")
    return (
        f"cd {infra_dir} && "
        f"docker compose -p {quoted_name} --env-file ../.env "
        f"-f compose.base.yml -f compose.prod.yml {subcommand}"
    )


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
                # A module we know how to check but cannot reach stays unverified,
                # which is a failure, not a skip.
                if module in SMOKE_CHECKED_MODULES:
                    detail = "No allocated resource found, module cannot be verified"
                    checks.append({"module": module, "result": "fail", "detail": detail})
                    errors.append(f"Smoke failed: {module} check — {detail}")
                else:
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

            if module == ServiceModule.BACKEND:
                check = await self._check_backend_health(server_ip, port)
                checks.append(check)
                if check["result"] == "fail":
                    errors.append(f"Smoke failed: backend health check — {check['detail']}")

            elif module == ServiceModule.TG_BOT:
                check = await self._check_tg_bot(state, resource)
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
            project_name = project_spec_runtime_slug(state.get("project_spec") or {})
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
        # Propagate bot_username from tg_bot check to state (for QA handoff)
        for check in checks:
            if check.get("bot_username"):
                result["bot_username"] = check["bot_username"]
                break
        if errors:
            result["errors"] = errors
        return result

    def _find_resource(self, allocated_resources: dict, module: str) -> dict | None:
        """Find allocated resource entry for a given module."""
        for alloc in allocated_resources.values():
            if isinstance(alloc, dict) and alloc.get("service_name") == module:
                return alloc
        return None

    async def _ssh_run(self, server_ip: str, server_handle: str, command: str) -> str:
        """Run a command on the project server over SSH and return stdout."""
        server = await api_client.get_server(server_handle)
        ssh_key = await api_client.get_server_ssh_key(server_handle)
        if not ssh_key:
            raise RuntimeError(f"No SSH key for server {server_handle}")

        key = asyncssh.import_private_key(ssh_key)
        async with asyncssh.connect(
            server_ip,
            username=server.ssh_user,
            known_hosts=None,
            client_keys=[key],
        ) as conn:
            result = await conn.run(command, check=False)
            return result.stdout.strip() if result.stdout else ""

    async def _fetch_container_logs(
        self,
        server_ip: str,
        server_handle: str,
        project_name: str,
    ) -> str | None:
        """SSH into server and fetch docker compose logs for the project.

        Returns log output (truncated) or None if fetch fails.
        """
        cmd = compose_command(project_name, f"logs --tail={CONTAINER_LOG_TAIL} --no-color 2>&1")
        try:
            return await self._ssh_run(server_ip, server_handle, cmd) or None
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
                            "module": ServiceModule.BACKEND.value,
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
            "module": ServiceModule.BACKEND.value,
            "result": "fail",
            "detail": last_error or "Unknown error",
        }

    async def _check_tg_bot(self, state: DevOpsState, resource: dict) -> dict:
        """Verify the deployed bot: Bot API identity plus a running tg_bot container.

        Both probes are mandatory. A missing token, server handle or SSH access is
        a failed check, never a silent skip: an unverified bot has to be visible in
        smoke_result.
        """
        try:
            bot_username = await self._resolve_bot_identity(state)
            container_detail = await self._check_bot_container(state, resource)
        except TgBotCheckFailed as e:
            logger.warning("smoke_tg_bot_fail", reason=str(e))
            return {
                "module": TG_BOT_SERVICE,
                "result": "fail",
                "detail": str(e),
            }

        return {
            "module": TG_BOT_SERVICE,
            "result": "pass",
            "detail": f"getMe → @{bot_username}, {container_detail}",
            "bot_username": bot_username,
        }

    async def _resolve_bot_identity(self, state: DevOpsState) -> str:
        """Call Bot API getMe — proves the token is live, yields the username."""
        bot_token = state.get("secret_values", {}).get("TELEGRAM_BOT_TOKEN")
        if not bot_token:
            raise TgBotCheckFailed("No TELEGRAM_BOT_TOKEN in secret_values")

        url = f"{BOT_API_BASE}/bot{bot_token}/getMe"
        last_error = None

        async with httpx.AsyncClient() as client:
            for attempt in range(HEALTH_CHECK_RETRIES):
                try:
                    response = await client.get(url, timeout=HEALTH_CHECK_TIMEOUT)
                    payload = response.json()
                    if response.status_code == HTTP_OK and payload.get("ok"):
                        return payload["result"]["username"]
                    last_error = (
                        f"getMe returned HTTP {response.status_code}: {payload.get('description')}"
                    )
                # ValueError covers a body the Bot API never sends but a proxy might
                except (httpx.HTTPError, ValueError) as e:
                    last_error = f"getMe request failed: {e}"

                if attempt < HEALTH_CHECK_RETRIES - 1:
                    logger.info("smoke_tg_bot_getme_retry", attempt=attempt + 1, error=last_error)
                    await asyncio.sleep(HEALTH_CHECK_RETRY_DELAY)

        raise TgBotCheckFailed(last_error)

    async def _check_bot_container(self, state: DevOpsState, resource: dict) -> str:
        """Confirm the tg_bot container is running on the project server."""
        server_handle = resource.get("server_handle")
        if not server_handle:
            raise TgBotCheckFailed("No server_handle in the allocated tg_bot resource")
        server_ip = resource["server_ip"]
        project_name = project_spec_runtime_slug(state.get("project_spec") or {})
        cmd = compose_command(project_name, "ps --services --status running")

        last_error = None
        for attempt in range(HEALTH_CHECK_RETRIES):
            try:
                running = (await self._ssh_run(server_ip, server_handle, cmd)).split()
            except Exception as e:
                last_error = f"container check over SSH failed: {e}"
                logger.warning("smoke_tg_bot_ssh_failed", server_ip=server_ip, exc_info=True)
            else:
                if TG_BOT_SERVICE in running:
                    return f"container {TG_BOT_SERVICE} is running"
                last_error = (
                    f"container {TG_BOT_SERVICE} is not running "
                    f"(running services: {', '.join(running) or 'none'})"
                )

            if attempt < HEALTH_CHECK_RETRIES - 1:
                logger.info("smoke_tg_bot_container_retry", attempt=attempt + 1, error=last_error)
                await asyncio.sleep(HEALTH_CHECK_RETRY_DELAY)

        raise TgBotCheckFailed(last_error)


smoke_tester_node = SmokeTesterNode()
