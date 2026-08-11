"""The QA executor's egress policy, at the level of its own decisions.

The network is what actually holds the boundary, and that is proven in
`tests/service/test_qa_egress_boundary.py` against a real Docker daemon. What
is proven here is the policy this runtime writes down: which destinations the
run's one door opens, what it refuses, and what the runtime refuses to start a
container without.
"""

from __future__ import annotations

import asyncio

import pytest

from shared.contracts.vocab import AgentType
from src import qa_egress
from src.qa_egress_proxy import (
    Refused,
    authorize,
    handle_client,
    parse_allowlist,
    parse_connect,
)


class TestTheDoorOpensOnlyTheModelBackend:
    def test_a_bare_host_means_https(self):
        assert parse_allowlist(["api.anthropic.com"]) == frozenset({("api.anthropic.com", 443)})

    def test_a_host_and_port_are_taken_as_written(self):
        assert parse_allowlist(["backend.test:8443"]) == frozenset({("backend.test", 8443)})

    def test_an_entry_that_is_not_a_destination_stops_the_proxy(self):
        """A silently dropped allowlist entry is a policy nobody can read."""
        with pytest.raises(ValueError, match="HOST"):
            parse_allowlist(["api.anthropic.com:https"])

    def test_an_empty_allowlist_stops_the_proxy(self):
        with pytest.raises(ValueError, match="opens nothing"):
            parse_allowlist([])

    def test_the_deployment_is_refused_like_any_other_host(self):
        allowed = parse_allowlist(["api.anthropic.com"])

        with pytest.raises(Refused) as refusal:
            authorize(allowed, "app.example.com", 443)

        assert refusal.value.status == "403 Forbidden"

    def test_the_allowed_backend_is_opened(self):
        allowed = parse_allowlist(["api.anthropic.com"])

        authorize(allowed, "api.anthropic.com", 443)

    def test_a_different_port_on_an_allowed_host_is_still_refused(self):
        allowed = parse_allowlist(["api.anthropic.com"])

        with pytest.raises(Refused):
            authorize(allowed, "api.anthropic.com", 8080)


class TestItIsNotAForwardProxy:
    """A CONNECT tunnel cannot carry a method; a forward proxy can carry POST."""

    @pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE", "GET"])
    def test_every_ordinary_method_is_refused(self, method):
        with pytest.raises(Refused) as refusal:
            parse_connect(f"{method} http://app.example.com/orders HTTP/1.1")

        assert refusal.value.status == "405 Method Not Allowed"

    def test_connect_is_read_as_a_host_and_port(self):
        assert parse_connect("CONNECT api.anthropic.com:443 HTTP/1.1") == (
            "api.anthropic.com",
            443,
        )

    def test_a_connect_without_a_port_is_refused(self):
        with pytest.raises(Refused) as refusal:
            parse_connect("CONNECT api.anthropic.com HTTP/1.1")

        assert refusal.value.status == "400 Bad Request"


class TestTheProxyAnswersOverARealSocket:
    """The refusal has to reach the client as HTTP, not just as an exception."""

    async def _speak(self, request: bytes) -> bytes:
        allowed = parse_allowlist(["api.anthropic.com"])

        async def _client(reader, writer):
            await handle_client(reader, writer, allowed)

        server = await asyncio.start_server(_client, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        async with server:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(request)
            await writer.drain()
            answer = await asyncio.wait_for(reader.read(4096), timeout=5)
            writer.close()
        return answer

    async def test_a_post_to_the_deployment_is_answered_with_405(self):
        answer = await self._speak(b"POST http://app.example.com/orders HTTP/1.1\r\nHost: app.example.com\r\n\r\n")

        assert answer.startswith(b"HTTP/1.1 405")

    async def test_a_tunnel_to_the_deployment_is_answered_with_403(self):
        answer = await self._speak(b"CONNECT app.example.com:443 HTTP/1.1\r\n\r\n")

        assert answer.startswith(b"HTTP/1.1 403")
        assert b"capability endpoint" in answer


class TestWhatTheRuntimeRefusesToStartWithout:
    async def test_a_network_that_is_not_internal_is_refused(self):
        class Docker:
            async def inspect_network(self, name):
                return {"Internal": False}

        with pytest.raises(qa_egress.QAEgressError, match="not internal"):
            await qa_egress.require_internal_network(Docker(), "codegen_qa_egress")

    async def test_an_internal_network_is_accepted(self):
        class Docker:
            async def inspect_network(self, name):
                return {"Internal": True}

        await qa_egress.require_internal_network(Docker(), "codegen_qa_egress")

    def test_a_container_on_exactly_the_run_network_is_accepted(self):
        qa_egress.verify_isolation({"NetworkSettings": {"Networks": {"codegen_qa_egress": {}}}}, "codegen_qa_egress")

    def test_a_container_with_a_second_network_is_refused(self):
        with pytest.raises(qa_egress.QAEgressError, match="codegen_worker"):
            qa_egress.verify_isolation(
                {"NetworkSettings": {"Networks": {"codegen_qa_egress": {}, "codegen_worker": {}}}},
                "codegen_qa_egress",
            )

    def test_a_container_on_no_network_at_all_is_refused(self):
        with pytest.raises(qa_egress.QAEgressError):
            qa_egress.verify_isolation({"NetworkSettings": {"Networks": {}}}, "codegen_qa_egress")


class TestWhichBackendsARunOpens:
    def test_claude_gets_the_anthropic_backend(self):
        assert qa_egress.model_backends(AgentType.CLAUDE) == (
            "api.anthropic.com",
            "statsig.anthropic.com",
        )

    def test_codex_gets_the_openai_backend(self):
        assert "chatgpt.com" in qa_egress.model_backends(AgentType.CODEX)
        assert "api.anthropic.com" not in qa_egress.model_backends(AgentType.CODEX)

    def test_an_operator_override_replaces_the_defaults(self):
        assert qa_egress.model_backends(AgentType.CLAUDE, "proxy.internal:8443") == ("proxy.internal:8443",)

    def test_an_agent_with_no_known_backend_stops_the_run(self):
        """An egress policy nobody can describe is not one to start a run with."""
        with pytest.raises(qa_egress.QAEgressError, match="no model backend"):
            qa_egress.model_backends(AgentType.NOOP)


class TestWhatTheExecutorIsToldAboutIt:
    def test_the_runtimes_own_services_are_not_sent_through_the_proxy(self):
        direct = qa_egress.direct_hosts(
            {"QA_CAPABILITY_URL": "http://qa-worker:41234/qa/call"},
            "http://worker-broker:8001",
        )

        assert direct == ("localhost", "127.0.0.1", "qa-worker", "worker-broker")

    def test_only_https_is_pointed_at_the_proxy(self):
        env = qa_egress.proxy_env("qa-egress-qa-1", ("qa-worker",))

        assert env["HTTPS_PROXY"] == env["https_proxy"] == "http://qa-egress-qa-1:3128"
        assert env["NO_PROXY"] == env["no_proxy"] == "qa-worker"
        # Plain HTTP is never proxied: everything the container speaks it to is
        # on its own network, and the broker channel must not depend on the door.
        assert "HTTP_PROXY" not in env

    def test_every_variable_this_sets_reaches_the_agent_process(self):
        """The container is not the consumer of these; the CLI child process is.

        worker-manager writes them into the container, the wrapper decides what
        the agent inherits, and the two lists are only useful if they are the
        same list. A variable added here and not there is an executor that
        cannot reach its backend — which is exactly how it went wrong once.
        """
        from worker_wrapper.wrapper import QA_EGRESS_PROXY_ENV

        assert set(qa_egress.proxy_env("qa-egress-qa-1", ("qa-worker",))) == set(QA_EGRESS_PROXY_ENV)
