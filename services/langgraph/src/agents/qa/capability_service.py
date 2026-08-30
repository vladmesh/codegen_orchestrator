"""Per-run typed QA endpoint; target credentials remain on the management host."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from dataclasses import dataclass
import inspect
import secrets

from aiohttp import web
import structlog

from shared.qa_probe_cli import CAPABILITIES_CALL, SUBMIT_VERDICT_CALL

logger = structlog.get_logger(__name__)

CALL_PATH = "/qa/call"
MAX_VERDICT_CHARS = 100_000


@dataclass(frozen=True)
class QACapabilityEndpoint:
    """What the executor container is told, and the whole of what it is given."""

    url: str
    token: str


class QACapabilityRejected(Exception):
    """The executor asked for something this endpoint does not serve."""


class QACapabilityService:
    """One run's capability endpoint. Started by the runner, stopped with the run."""

    def __init__(
        self,
        *,
        calls: Mapping[str, Callable],
        capabilities: dict,
        submit_verdict: Callable[[str], None],
        advertised_host: str,
        bind_host: str = "0.0.0.0",  # noqa: S104 — reachable from the executor's network
        port: int = 0,
    ) -> None:
        self._calls = dict(calls)
        self._capabilities = capabilities
        self._submit_verdict = submit_verdict
        self._advertised_host = advertised_host
        self._bind_host = bind_host
        self._port = port
        self._token = secrets.token_urlsafe(32)
        self._runner: web.AppRunner | None = None
        self.verdict_received = asyncio.Event()
        # Distinguishes an executor failure from a run with no verdict.
        self.calls_served = 0

    @property
    def token(self) -> str:
        return self._token

    async def start(self) -> QACapabilityEndpoint:
        app = web.Application()
        app.add_routes([web.post(CALL_PATH, self._handle_call)])
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self._bind_host, self._port)
        await site.start()
        port = self._runner.addresses[0][1]
        endpoint = QACapabilityEndpoint(
            url=f"http://{self._advertised_host}:{port}{CALL_PATH}",
            token=self._token,
        )
        logger.info("qa_capability_endpoint_started", url=endpoint.url)
        return endpoint

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
            logger.info("qa_capability_endpoint_stopped")

    async def _handle_call(self, request: web.Request) -> web.Response:
        if not self._authorized(request):
            logger.warning("qa_capability_unauthorized", peer=request.remote)
            return web.json_response({"error": "unauthorized"}, status=401)
        try:
            payload = await request.json()
        except ValueError:
            return web.json_response({"error": "body is not JSON"}, status=400)
        if not isinstance(payload, dict):
            return web.json_response({"error": "body is not a call object"}, status=400)

        name = payload.get("tool")
        args = payload.get("args") or {}
        if not isinstance(name, str) or not isinstance(args, dict):
            return web.json_response(
                {"error": "a call is {'tool': <name>, 'args': {<name>: <value>}}"}, status=400
            )
        try:
            body = await self._dispatch(name, args)
        except QACapabilityRejected as exc:
            logger.info("qa_capability_refused", tool=name, error=str(exc))
            return web.json_response({"tool": name, "error": str(exc)}, status=400)
        return web.json_response(body)

    def _authorized(self, request: web.Request) -> bool:
        header = request.headers.get("Authorization", "")
        scheme, _, presented = header.partition(" ")
        if scheme.lower() != "bearer" or not presented:
            return False
        return secrets.compare_digest(presented, self._token)

    async def _dispatch(self, name: str, args: dict) -> dict:
        if name == CAPABILITIES_CALL:
            return {"tool": name, **self._capabilities}
        if name == SUBMIT_VERDICT_CALL:
            return self._accept_verdict(args)

        call = self._calls.get(name)
        if call is None:
            raise QACapabilityRejected(
                f"{name} is not a call this run has; available: "
                f"{', '.join(sorted([*self._calls, CAPABILITIES_CALL, SUBMIT_VERDICT_CALL]))}"
            )
        self._check_arguments(name, call, args)
        self.calls_served += 1
        value = call(**args)
        if inspect.isawaitable(value):
            value = await value
        if isinstance(value, dict):
            return {"tool": name, **value}
        return {"tool": name, "result": value}

    @staticmethod
    def _check_arguments(name: str, call: Callable, args: dict) -> None:
        """Bind arguments to the callable signature without widening the API."""
        try:
            inspect.signature(call).bind(**args)
        except TypeError as exc:
            raise QACapabilityRejected(f"{name}: {exc}") from exc

    def _accept_verdict(self, args: dict) -> dict:
        raw = args.get("result")
        if not isinstance(raw, str) or not raw.strip():
            raise QACapabilityRejected("submit_qa_result needs the result JSON as `result`")
        if len(raw) > MAX_VERDICT_CHARS:
            raise QACapabilityRejected(
                f"the result must be under {MAX_VERDICT_CHARS} characters; "
                "the report belongs in `qa report`"
            )
        self._submit_verdict(raw)
        self.verdict_received.set()
        logger.info("qa_capability_verdict_received", chars=len(raw))
        return {"tool": SUBMIT_VERDICT_CALL, "result": "verdict recorded; the run is over"}
