"""Smoke tester node for post-deploy verification.

Runs deterministic health checks after deployment:
- Backend modules: GET /health → HTTP 200
- Telegram bot modules: Telethon /start → non-empty response (step 3)
"""

import asyncio

import httpx
import structlog

from ...nodes.base import FunctionalNode, RetryPolicy
from .state import DevOpsState

logger = structlog.get_logger()

HEALTH_CHECK_TIMEOUT = 10
HEALTH_CHECK_RETRIES = 3
HEALTH_CHECK_RETRY_DELAY = 5
HTTP_OK = 200


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
        """Telegram bot smoke check (placeholder — implemented in step 3)."""
        return {
            "module": "tg_bot",
            "result": "skip",
            "detail": "Telethon check not yet implemented",
        }


smoke_tester_node = SmokeTesterNode()
