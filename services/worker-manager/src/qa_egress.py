"""What a QA executor container can reach, as a property of the network.

Until this module existed, the guarantee "exploratory QA does not write to the
application" rested on the executor having no tool that could express a write.
The executor is a CLI coding agent now: it has a shell, `curl`, and — on an
ordinary worker network — an ordinary route to the internet. A rule in the
prompt and a scan of the transcript afterwards are not a boundary; they are a
request and a receipt.

The boundary is here, and it is made of two things:

1. **An `internal` Docker network.** The QA executor container is attached to
   exactly one network, and that network has no route off itself. The
   deployment's public URL, its IP, the management host, the rest of the fleet
   and the internet are not forbidden to the container — they are unreachable
   from it. `docker network create --internal` is what does this, and
   `verify_isolation` refuses to let a run start on a container that ended up
   anywhere else.
2. **One CONNECT-only proxy, allowlisted to the assigned CLI's model backend.**
   Claude Code and Codex have to talk to their own backends or there is no
   executor at all, so exactly that much is opened, per run, by
   `qa_egress_proxy`. It tunnels `CONNECT host:port` for the hosts named on its
   command line and refuses everything else, including every non-CONNECT
   method — so it cannot be turned back into a general forward proxy.

The application under test is reachable from neither. It stays reachable only
through the runtime's typed capability endpoint, which is served on the same
internal network by the QA runtime itself and is GET-only for the public URL.

Fail-closed is the point: every check below raises instead of degrading. A run
whose egress policy did not establish does not start with an unrestricted
container — worker creation fails, and the QA runtime turns that into the same
typed QA-infrastructure outcome as any other executor that could not run.

Developer workers are untouched. They keep `WORKER_NETWORK` and its ordinary
connectivity; this is the QA worker's network, not the shared one.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import structlog

from shared.contracts.vocab import AgentType

from .qa_egress_proxy import LISTEN_PORT as PROXY_PORT

logger = structlog.get_logger()

PROXY_NAME_PREFIX = "qa-egress-"
PROXY_TYPE_LABEL = "qa-egress-proxy"
PROXY_MEM_LIMIT = "256m"
PROXY_PIDS_LIMIT = 64
PROXY_READY_ATTEMPTS = 30
PROXY_READY_DELAY = 0.5

# Sent as the source of the proxy process, because the proxy runs in a container
# built from the QA executor's own image and that image carries nothing of this
# repository. Reading the module's own file keeps one copy of the proxy: the one
# its unit tests import.
PROXY_SOURCE = (Path(__file__).with_name("qa_egress_proxy.py")).read_text()

# Hosts the assigned CLI cannot work without. Nothing else is opened, and a
# QA executor is only ever Claude Code or Codex.
DEFAULT_MODEL_BACKENDS: dict[AgentType, tuple[str, ...]] = {
    AgentType.CLAUDE: ("api.anthropic.com", "statsig.anthropic.com"),
    AgentType.CODEX: ("chatgpt.com", "api.openai.com", "auth.openai.com"),
}


class QAEgressError(RuntimeError):
    """The run's egress policy did not establish, so no container may run."""


@dataclass(frozen=True)
class QAEgress:
    """The established policy, and what the executor container is told about it."""

    network: str
    proxy_container_id: str
    proxy_host: str
    allowed: tuple[str, ...]
    env_vars: dict[str, str]


def proxy_container_name(worker_id: str) -> str:
    return f"{PROXY_NAME_PREFIX}{worker_id}"


def model_backends(agent_type: AgentType, configured: str = "") -> tuple[str, ...]:
    """The `host[:port]` list the run's proxy will open, and nothing besides.

    An operator may name the backends explicitly; an empty setting means the
    defaults for the assigned agent. An agent with no known backend is a
    configuration error and stops the run rather than starting a container with
    an empty door.
    """
    if configured.strip():
        hosts = tuple(entry.strip() for entry in configured.split(",") if entry.strip())
    else:
        hosts = DEFAULT_MODEL_BACKENDS.get(agent_type, ())
    if not hosts:
        raise QAEgressError(
            f"no model backend is configured for a {agent_type} QA executor; "
            f"a QA run cannot open an egress policy it cannot describe"
        )
    return hosts


def direct_hosts(env_vars: dict[str, str], broker_url: str) -> tuple[str, ...]:
    """Hosts the container addresses on its own network, never through the proxy.

    These are the QA runtime's per-run capability endpoint and the worker broker.
    Both live on the internal network with the container; sending them through
    the proxy would only get them refused.
    """
    hosts = ["localhost", "127.0.0.1"]
    for url in (env_vars.get("QA_CAPABILITY_URL", ""), broker_url):
        host = urlsplit(url).hostname
        if host:
            hosts.append(host)
    return tuple(dict.fromkeys(hosts))


def proxy_env(proxy_host: str, no_proxy: tuple[str, ...]) -> dict[str, str]:
    """The proxy variables the CLI reads.

    They are a convenience for the CLI, not the boundary: an executor that
    ignores or unsets them reaches nothing at all, because the network it is on
    has no other way out. That is the intended failure — closed, not open.

    Only the HTTPS variables are set. Every destination the proxy opens is
    HTTPS, and everything the container speaks plain HTTP to — the capability
    endpoint, the broker — is on its own network, where a proxy would only get
    in the way. `NO_PROXY` names them anyway, for clients that read `ALL_PROXY`
    out of an operator's own environment.
    """
    url = f"http://{proxy_host}:{PROXY_PORT}"
    joined = ",".join(no_proxy)
    return {
        "HTTPS_PROXY": url,
        "https_proxy": url,
        "NO_PROXY": joined,
        "no_proxy": joined,
    }


async def require_internal_network(docker, network: str) -> None:
    """Refuse to start a QA run on a network that can route off itself."""
    try:
        attrs = await docker.inspect_network(network)
    except Exception as exc:  # noqa: BLE001 — absence and API failure both mean "no policy"
        raise QAEgressError(
            f"the QA egress network {network!r} could not be inspected ({exc}); "
            f"a QA executor is not started without one"
        ) from exc
    if not attrs.get("Internal"):
        raise QAEgressError(
            f"the QA egress network {network!r} is not internal: a QA executor on it "
            f"would reach the deployment directly. Declare it with `internal: true`."
        )


def verify_isolation(attrs: dict, network: str) -> None:
    """Refuse a container that is attached to anything but the run's network."""
    attached = set((attrs.get("NetworkSettings") or {}).get("Networks") or {})
    if attached != {network}:
        raise QAEgressError(
            f"the QA executor container is attached to {sorted(attached)}, "
            f"but its egress policy is the single network {network!r}"
        )


async def establish(
    docker,
    *,
    worker_id: str,
    agent_type: AgentType,
    image: str,
    network: str,
    internet_network: str,
    configured_backends: str,
    direct: tuple[str, ...],
    labels: dict[str, str] | None = None,
) -> QAEgress:
    """Put this run's egress policy in place, or raise so the run does not start.

    Order matters. The network is proven internal before anything is created,
    and the proxy is proven to be listening before the executor container that
    depends on it is built — a container started against a proxy that never came
    up would look to the CLI exactly like a broken session and would be retried
    as one.
    """
    await require_internal_network(docker, network)
    allowed = model_backends(agent_type, configured_backends)
    name = proxy_container_name(worker_id)
    proxy_labels = dict(labels or {})
    proxy_labels.update({"com.codegen.type": PROXY_TYPE_LABEL, "com.codegen.worker.id": worker_id})

    await docker.remove_container(name, force=True)
    try:
        container = await docker.run_container(
            image=image,
            name=name,
            hostname=name,
            entrypoint=["python3", "-c", PROXY_SOURCE],
            command=list(allowed),
            detach=True,
            network=network,
            labels=proxy_labels,
            mem_limit=PROXY_MEM_LIMIT,
            pids_limit=PROXY_PIDS_LIMIT,
            cap_drop=["ALL"],
            security_opt=["no-new-privileges:true"],
        )
        # The second leg. The proxy needs the connectivity the executor must not
        # have, so it — and only it — is also on a network that has a route out.
        await docker.connect_network(internet_network, container.id)
        await _await_proxy(docker, container.id, name)
    except Exception as exc:
        await docker.remove_container(name, force=True)
        if isinstance(exc, QAEgressError):
            raise
        raise QAEgressError(f"the QA egress proxy for {worker_id} did not start: {exc}") from exc

    logger.info("qa_egress_established", worker_id=worker_id, network=network, allowed=allowed)
    return QAEgress(
        network=network,
        proxy_container_id=container.id,
        proxy_host=name,
        allowed=tuple(allowed),
        env_vars=proxy_env(name, direct),
    )


async def _await_proxy(docker, container_id: str, name: str) -> None:
    """Wait until the proxy is accepting connections, from inside its container.

    The management host is not on the run's internal network, so this is the
    only place the check can be made from. A proxy that never listens is a
    policy that did not establish.
    """
    probe = f"python3 -c \"import socket; socket.create_connection(('127.0.0.1', {PROXY_PORT}), 2)\""
    for _ in range(PROXY_READY_ATTEMPTS):
        try:
            exit_code, _ = await docker.exec_in_container(container_id, probe, user="root")
        except Exception:  # noqa: BLE001 — a container still coming up is not yet a failure
            exit_code = 1
        if exit_code == 0:
            return
        await asyncio.sleep(PROXY_READY_DELAY)
    logs = await docker.get_container_logs(container_id)
    raise QAEgressError(f"the QA egress proxy {name} never accepted a connection: {logs}")


async def tear_down(docker, worker_id: str) -> None:
    """Remove the run's proxy. Called on every way out, including a failed start."""
    name = proxy_container_name(worker_id)
    try:
        await docker.remove_container(name, force=True)
    except Exception as exc:  # noqa: BLE001 — a proxy that outlives its run is a warning, not a crash
        logger.warning("qa_egress_proxy_removal_failed", worker_id=worker_id, error=str(exc))
    else:
        logger.info("qa_egress_proxy_removed", worker_id=worker_id)
