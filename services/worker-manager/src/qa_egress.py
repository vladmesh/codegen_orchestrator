"""What a QA executor container can reach, as a property of the network.

The QA executor is a sandbox: a CLI coding agent with a shell, `python3`, `curl`
and Telethon, which may run its own scripts against the product under test. What
keeps that safe is not what the agent is told but where its packets can go, and
that is decided here, per run, by two things:

1. **An `internal` Docker network.** The QA executor container is attached to
   exactly one network, and that network has no route off itself. The
   management host, the platform's own services, the rest of the fleet and the
   internet are not forbidden to the container — they are unreachable from it.
   `docker network create --internal` is what does this, and `verify_isolation`
   refuses to let a run start on a container that ended up anywhere else.
2. **One CONNECT-only proxy with a per-run allowlist.** `qa_egress_proxy`
   tunnels `CONNECT host:port` to exactly three kinds of destination and
   refuses everything else, including every non-CONNECT method — so it cannot
   be turned back into a general forward proxy:

   * the assigned CLI's model backend (`model_backends`);
   * the host of the run's deployed public URL, on the URL's explicit port or
     on 443 and 80 (`deploy_target_entries`). The network does not tell a read
     from a write there: a CONNECT tunnel carries whatever the executor sends.
     That no direct application-API write is made is policy, not routing —
     the QA instructions allow GETs only, and the runtime's evidence guard
     (`_forbidden_application_write` in qa-worker) fails a run whose report,
     result or transcript shows one. It stays so until product-data isolation
     (ephemeral product stands) exists;
   * Telegram's MTProto data centres (`TELEGRAM_MTPROTO_NETWORKS`), so a
     Telethon client logged in as the QA account can talk to the bot.

What the sandbox does not get is as much a part of the boundary: no SSH key and
no route to the target's port 22 (every SSH-based capability stays in the QA
runtime, behind its typed capability endpoint), no platform service, and no
secret of the platform in its environment or mounts.

The deploy target arrives as data on the create request and is refused, before
any container exists, when it could point the door back at the platform: an
empty value, a URL carrying userinfo, a name the container reaches directly, a
single-label service name, or a loopback, link-local or private literal.

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
import ipaddress
from pathlib import Path
from urllib.parse import urlsplit

import structlog

from shared.contracts.queues.worker import QA_TARGET_REFUSED, WorkerLabel
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


# Telegram's MTProto data centres, as Telegram publishes them
# (https://core.telegram.org/resources/cidr.txt), and the ports MTProto is served
# on. IPv4 only: Telethon dials the IPv4 data centres unless told otherwise, and
# the QA network is IPv4. This is the one place the list lives.
TELEGRAM_MTPROTO_NETWORKS: tuple[str, ...] = (
    "91.105.192.0/23",
    "91.108.4.0/22",
    "91.108.8.0/22",
    "91.108.12.0/22",
    "91.108.16.0/22",
    "91.108.20.0/22",
    "91.108.56.0/22",
    "149.154.160.0/20",
    "185.76.151.0/24",
)
TELEGRAM_MTPROTO_PORTS: tuple[int, ...] = (443, 80, 5222)

# Ports opened on a deploy target whose URL names none: HTTPS and plain HTTP.
DEPLOY_TARGET_DEFAULT_PORTS: tuple[int, ...] = (443, 80)
_DEPLOY_TARGET_SCHEMES = frozenset({"http", "https"})
# Suffixes that name something on a private network, never a public deployment.
_LOCAL_NAME_SUFFIXES = (".localhost", ".local", ".internal", ".localdomain")


class QAEgressError(RuntimeError):
    """The run's egress policy did not establish, so no container may run."""


class QATargetRefused(QAEgressError):
    """The run's deploy target is not a destination the sandbox may be opened to.

    The message leads with `QA_TARGET_REFUSED`, so the QA runtime reading the
    worker's error text knows the refusal is deterministic and does not retry.
    """

    def __init__(self, detail: str) -> None:
        super().__init__(f"{QA_TARGET_REFUSED}: {detail}")


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


def telegram_entries() -> tuple[str, ...]:
    """Telegram's data centres as proxy allowlist entries, one per network and port."""
    return tuple(
        f"{network}:{port}"
        for network in TELEGRAM_MTPROTO_NETWORKS
        for port in TELEGRAM_MTPROTO_PORTS
    )


def deploy_target_entries(target_url: str | None, direct: tuple[str, ...]) -> tuple[str, ...]:
    """The allowlist entries that open the run's deploy target, or a refusal.

    `target_url` is the run's deployed public URL, as the QA runtime sent it.
    Only its host is opened, on the URL's explicit port or on 443 and 80. It is
    refused when it could turn the door back towards the platform: empty, not
    an http(s) URL, carrying userinfo, a name in `direct` (what the container
    already reaches on its own network), a single-label or local name (a
    service on the platform's own networks), or an IP literal that is not a
    public address (loopback, link-local, RFC 1918 and the like).
    """
    raw = (target_url or "").strip()
    if not raw:
        raise QATargetRefused("a QA run needs its deployed public URL; none was sent")
    parts = urlsplit(raw)
    if parts.scheme not in _DEPLOY_TARGET_SCHEMES or not parts.netloc:
        raise QATargetRefused(f"the QA deploy target {raw!r} is not an http(s) URL")
    if "@" in parts.netloc:
        raise QATargetRefused("the QA deploy target URL carries userinfo; it is refused")
    try:
        explicit_port = parts.port
    except ValueError as exc:
        raise QATargetRefused(f"the QA deploy target {raw!r} has an invalid port") from exc
    # The authority names a port when a `:` follows the host (after any IPv6
    # brackets). Such a port must be a real one: `host:` and `host:0` are
    # malformed, never a request for the defaults.
    names_port = ":" in parts.netloc.rpartition("]")[2]
    if names_port and not explicit_port:
        raise QATargetRefused(f"the QA deploy target {raw!r} names an empty or zero port")
    host = (parts.hostname or "").rstrip(".").lower()
    if not host:
        raise QATargetRefused(f"the QA deploy target {raw!r} names no host")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None:
        if not address.is_global:
            raise QATargetRefused(
                f"the QA deploy target {host} is not a public address; the sandbox is "
                f"never opened to a loopback, link-local or private network"
            )
    elif (
        host in {name.lower() for name in direct}
        or host == "localhost"
        or "." not in host
        or host.endswith(_LOCAL_NAME_SUFFIXES)
    ):
        raise QATargetRefused(
            f"the QA deploy target {host!r} names a service on the platform's own "
            f"networks, not a public deployment"
        )
    ports = (explicit_port,) if names_port else DEPLOY_TARGET_DEFAULT_PORTS
    spelled = f"[{host}]" if isinstance(address, ipaddress.IPv6Address) else host
    return tuple(f"{spelled}:{port}" for port in ports)


def proxy_env(proxy_host: str, no_proxy: tuple[str, ...]) -> dict[str, str]:
    """The proxy variables the CLI reads.

    They are a convenience for the CLI, not the boundary: an executor that
    ignores or unsets them reaches nothing at all, because the network it is on
    has no other way out. That is the intended failure — closed, not open.

    Only the HTTPS variables are set. The proxy speaks CONNECT alone, and a
    client told `HTTP_PROXY` would send it absolute-form plain HTTP that it
    refuses; a plain-`http://` deployment is reached through an explicit
    CONNECT tunnel instead (`curl --proxytunnel -x "$HTTPS_PROXY" http://…`).
    Everything the container speaks plain HTTP to on its own network — the
    capability endpoint, the broker — is named in `NO_PROXY`.
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
    except Exception as exc:
        raise QAEgressError(
            f"the QA egress network {network!r} could not be inspected ({exc}); "
            f"a QA executor is not started without one"
        ) from exc
    if not attrs.get("Internal"):
        raise QAEgressError(
            f"the QA egress network {network!r} is not internal: a QA executor on it "
            f"would reach past its allowlist. Declare it with `internal: true`."
        )


def verify_isolation(attrs: dict, network: str) -> None:
    """Refuse a container that is attached to anything but the run's network."""
    attached = set((attrs.get("NetworkSettings") or {}).get("Networks") or {})
    if attached != {network}:
        raise QAEgressError(
            f"the QA executor container is attached to {sorted(attached)}, "
            f"but its egress policy is the single network {network!r}"
        )


async def establish(  # noqa: PLR0913 — one run's whole policy, each part named
    docker,
    *,
    worker_id: str,
    agent_type: AgentType,
    image: str,
    network: str,
    internet_network: str,
    configured_backends: str,
    direct: tuple[str, ...],
    deploy_target: tuple[str, ...],
    telegram: tuple[str, ...],
    labels: dict[str, str] | None = None,
) -> QAEgress:
    """Put this run's egress policy in place, or raise so the run does not start.

    Order matters. The network is proven internal before anything is created,
    and the proxy is proven to be listening before the executor container that
    depends on it is built — a container started against a proxy that never came
    up would look to the CLI exactly like a broken session and would be retried
    as one.

    `deploy_target` is `deploy_target_entries` of the run's URL and `telegram`
    is `telegram_entries()`; both are passed in so a test can stand local
    listeners in for them on the identical code path.
    """
    await require_internal_network(docker, network)
    if not deploy_target:
        raise QATargetRefused("a QA run's egress policy has no deploy target to open")
    allowed = (
        *model_backends(agent_type, configured_backends),
        *deploy_target,
        *telegram,
    )
    name = proxy_container_name(worker_id)
    proxy_labels = dict(labels or {})
    proxy_labels.update({WorkerLabel.TYPE.value: PROXY_TYPE_LABEL, WorkerLabel.ID.value: worker_id})

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
    probe = (
        f"python3 -c \"import socket; socket.create_connection(('127.0.0.1', {PROXY_PORT}), 2)\""
    )
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
