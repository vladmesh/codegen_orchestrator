"""Target-scoped, read-only QA operations and one-shot SSH grant handling."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
import datetime as dt
import re
import shlex
from typing import Protocol
import uuid

import asyncssh
import structlog

from shared.contracts.dto.qa_ssh_grant import QASshGrant, QASshGrantState

from ..runtime_identity import SERVICE_BASE_DIR

logger = structlog.get_logger(__name__)

GRANT_MARKER_PREFIX = "codegen-qa-run"
# SSHD expiry backs up runner and sweep grant removal.
GRANT_LIFETIME_S = 3600
REMOTE_EXEC_TIMEOUT = 60
LOCALHOST_PROBE_TIMEOUT = 30
MAX_REMOTE_OUTPUT = 8000
MAX_PORT = 65535
STATUS_MARKER = "<<qa-http-status:"
# Contained-read refusal statuses.
READ_UNRESOLVABLE = 3
READ_OUTSIDE_ROOT = 4
# Provisioning-identity refusal statuses.
IDENTITY_ABSENT = 3
IDENTITY_KEYS_ABSENT = 4
# Docker's authoritative deployment-container label.
COMPOSE_PROJECT_LABEL = "com.docker.compose.project"

# Shared retry policy for target Docker reads.
CONTAINER_PROBE_ATTEMPTS = 3
CONTAINER_PROBE_RETRY_DELAY = 5

# The restricted target wrapper enforces read-only Docker access.
QA_DOCKER_WRAPPER = "/usr/local/bin/qa-docker"
QA_DOCKER = ("sudo", "-n", QA_DOCKER_WRAPPER)

# Each Docker command must name a container in the run capability set.
CONTAINER_SCOPED_DOCKER = frozenset({"diff", "inspect", "logs", "port", "stats", "top"})

# Secret paths remain forbidden inside the physical deployment root.
SECRET_FILE_PATTERNS = (
    re.compile(r"(^|/)\.env(\.|$)"),
    re.compile(r"\.(pem|key|p12|pfx)$"),
    re.compile(r"(^|/)(credentials|secrets)[^/]*$", re.IGNORECASE),
    re.compile(r"(^|/)\.ssh(/|$)"),
)

# Resolve the requested path and require physical-root containment.
_CONTAINED_READ = """
set -eu
root=$1; requested=$2; limit=$3
case "$requested" in
  /*) candidate=$requested ;;
  *) candidate=$root/$requested ;;
esac
resolved=$(readlink -f -- "$candidate") || { echo "unresolvable" >&2; exit 3; }
case "$resolved" in
  "$root"|"$root"/*) ;;
  *) echo "outside:$resolved" >&2; exit 4 ;;
esac
[ -f "$resolved" ] || { echo "notafile:$resolved" >&2; exit 5; }
head -c "$limit" -- "$resolved"
"""

# Append only to an account and authorized_keys file provisioning already created.
_INSTALL_GRANT = """
set -eu
user=$1; entry=$2
home=$(getent passwd "$user" | cut -d: -f6)
[ -n "$home" ] || { echo "no such account: $user" >&2; exit 3; }
keys=$home/.ssh/authorized_keys
[ -f "$keys" ] || { echo "no authorized_keys for $user" >&2; exit 4; }
exec 9>"$home/.ssh/.codegen-qa.lock"
flock 9
printf '%s\\n' "$entry" >> "$keys"
"""

# Preserve authorized_keys when filtering produces no replacement content.
_REVOKE_GRANT = """
set -eu
user=$1; marker=$2
home=$(getent passwd "$user" | cut -d: -f6)
[ -n "$home" ] || { echo 0; exit 0; }
keys=$home/.ssh/authorized_keys
[ -f "$keys" ] || { echo 0; exit 0; }
exec 9>"$home/.ssh/.codegen-qa.lock"
flock 9
grep -v -F "$marker" "$keys" > "$keys.qa-tmp" || true
if [ -s "$keys.qa-tmp" ]; then cat "$keys.qa-tmp" > "$keys"; fi
rm -f "$keys.qa-tmp"
grep -c -F "$marker" "$keys" || true
"""


class QATargetError(RuntimeError):
    """A QA tool was asked for something outside the run's capability set."""


class QAGrantError(RuntimeError):
    """The one-shot identity for this run could not be issued."""


class QAIdentityAbsentError(QAGrantError):
    """The target lacks the provisioning-owned QA account or authorized_keys."""


class QACapabilityError(RuntimeError):
    """The run's capability set could not be resolved from the target."""


class QAContainerRuntimeError(QACapabilityError):
    """Target Docker did not answer a deployment-container query."""


@dataclass(frozen=True)
class QATarget:
    """Where the run's one deployment lives, and how to address it."""

    server_ip: str
    # Used only to issue and revoke the unprivileged QA identity.
    ssh_user: str
    # Provisioned unprivileged account used for the QA session.
    qa_ssh_user: str
    server_handle: str
    project_name: str
    deployed_url: str
    # Allocated loopback ports are the only ports QA may probe.
    allocated_ports: frozenset[int] = frozenset()
    bot_username: str | None = None

    @property
    def service_dir(self) -> str:
        return f"{SERVICE_BASE_DIR}/{self.project_name}"


@dataclass(frozen=True)
class QACapabilities:
    """Everything this run may see, and the only source of any tool's boundary."""

    deployed_url: str
    # Target-resolved root prevents symlinks from widening access.
    physical_root: str
    containers: frozenset[str]
    loopback_ports: frozenset[int]
    # The Telegram capability addresses only this bot.
    bot_username: str | None = None

    def describe(self) -> dict:
        return {
            "deployed_url": self.deployed_url,
            "physical_root": self.physical_root,
            "containers": sorted(self.containers),
            "loopback_ports": sorted(self.loopback_ports),
            "bot_username": self.bot_username,
        }


@dataclass(frozen=True)
class RemoteResult:
    """What one typed remote operation returned."""

    exit_status: int
    stdout: str
    stderr: str

    def as_dict(self) -> dict:
        return {
            "exit_status": self.exit_status,
            "stdout": self.stdout[:MAX_REMOTE_OUTPUT],
            "stderr": self.stderr[:MAX_REMOTE_OUTPUT],
        }


class QAGrantJournal(Protocol):
    """Where the fact of a grant is written down, before it is acted on."""

    async def write(self, grant: QASshGrant) -> None: ...


def _on_target(argv: list[str]) -> list[str]:
    """Route Docker through the target's restricted wrapper."""
    if argv and argv[0] == "docker":
        return [*QA_DOCKER, *argv[1:]]
    return argv


def _reject_secret_path(path: str) -> None:
    for pattern in SECRET_FILE_PATTERNS:
        if pattern.search(path):
            raise QATargetError(
                f"{path} holds deployment credentials; QA tests the running application, "
                "not the secrets it was deployed with"
            )


async def resolve_capabilities(
    conn: asyncssh.SSHClientConnection, target: QATarget
) -> QACapabilities:
    """Resolve one fixed capability set before any target operation exists."""
    root = await conn.run(f"readlink -f -- {shlex.quote(target.service_dir)}", check=False)
    physical_root = (root.stdout or "").strip()
    if root.exit_status != 0 or not physical_root:
        raise QACapabilityError(
            f"{target.service_dir} does not resolve on {target.server_ip}: "
            f"{(root.stderr or '').strip()[:300] or 'no such directory'}"
        )

    command = " ".join(
        shlex.quote(part)
        for part in [
            *QA_DOCKER,
            "ps",
            "--all",
            "--no-trunc",
            "--filter",
            f"label={COMPOSE_PROJECT_LABEL}={target.project_name}",
            "--format",
            "{{.Names}}",
        ]
    )
    listing = None
    failure = ""
    for attempt in range(CONTAINER_PROBE_ATTEMPTS):
        if attempt:
            await asyncio.sleep(CONTAINER_PROBE_RETRY_DELAY)
        try:
            listing = await conn.run(command, check=False)
        except (OSError, asyncssh.Error) as exc:
            failure = f"the target did not answer docker ps of {target.project_name}: {exc}"
            continue
        if listing.exit_status == 0:
            failure = ""
            break
        failure = (
            f"docker ps of {target.project_name} exited {listing.exit_status}: "
            f"{(listing.stderr or listing.stdout or 'no output').strip()[:300]}"
        )
    if failure:
        logger.error("qa_container_listing_unavailable", server_ip=target.server_ip, detail=failure)
        raise QAContainerRuntimeError(
            f"cannot list the containers of {target.project_name} on {target.server_ip}: {failure}"
        )
    containers = frozenset(
        name.strip() for name in (listing.stdout or "").splitlines() if name.strip()
    )

    capabilities = QACapabilities(
        deployed_url=target.deployed_url,
        physical_root=physical_root,
        containers=containers,
        loopback_ports=frozenset(target.allocated_ports),
        bot_username=target.bot_username,
    )
    logger.info("qa_capabilities_resolved", server_ip=target.server_ip, **capabilities.describe())
    return capabilities


class QATargetSession:
    """The typed surface a QA run has on its single deployment."""

    def __init__(
        self,
        target: QATarget,
        conn: asyncssh.SSHClientConnection,
        capabilities: QACapabilities,
    ) -> None:
        self._target = target
        self._conn = conn
        self._capabilities = capabilities

    @property
    def target(self) -> QATarget:
        return self._target

    @property
    def capabilities(self) -> QACapabilities:
        return self._capabilities

    def check_container(self, container: str) -> str:
        """Return a container that is in this run's container capability."""
        if container not in self._capabilities.containers:
            known = ", ".join(sorted(self._capabilities.containers)) or "(none)"
            raise QATargetError(
                f"{container} is not a container of this run's deployment; "
                f"this run can see: {known}"
            )
        return container

    def check_port(self, port: int) -> int:
        """Return a loopback port that is in this run's port capability."""
        if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= MAX_PORT:
            raise QATargetError(f"{port!r} is not a port")
        if port not in self._capabilities.loopback_ports:
            known = ", ".join(str(p) for p in sorted(self._capabilities.loopback_ports)) or "(none)"
            raise QATargetError(
                f"port {port} is not allocated to this run's deployment; "
                f"this run can reach: {known}"
            )
        return port

    async def _run(self, argv: list[str], *, timeout: int) -> RemoteResult:
        command = " ".join(shlex.quote(part) for part in _on_target(argv))
        result = await self._conn.run(command, check=False, timeout=timeout)
        return RemoteResult(
            exit_status=result.exit_status if result.exit_status is not None else -1,
            stdout=result.stdout or "",
            stderr=result.stderr or "",
        )

    async def read_file(self, path: str, *, max_bytes: int = MAX_REMOTE_OUTPUT) -> RemoteResult:
        """Read one file, resolved on the target, from inside the physical root.

        Resolution and containment happen on the target, in the same command as
        the read: a lexical check here would be satisfied by a symlink that
        points anywhere, and a separate resolve call would answer about a path
        the read no longer uses.
        """
        if re.search(r"\s", path):
            raise QATargetError(f"{path!r} is not a path")
        _reject_secret_path(path)
        result = await self._run(
            [
                "sh",
                "-c",
                _CONTAINED_READ,
                "_",
                self._capabilities.physical_root,
                path,
                str(max_bytes),
            ],
            timeout=REMOTE_EXEC_TIMEOUT,
        )
        if result.exit_status == READ_OUTSIDE_ROOT:
            raise QATargetError(
                f"{path} resolves outside this run's deployment "
                f"({self._capabilities.physical_root})"
            )
        if result.exit_status == READ_UNRESOLVABLE:
            raise QATargetError(f"{path} does not exist on the target")
        return result

    async def exec(self, argv: list[str]) -> RemoteResult:
        """Run one read-only docker command against a container of this deployment.

        The command's boundary is the container capability: it must name a
        container this run owns, and the sub-command must be one that only
        reads. There is no form of this call that describes the host.
        """
        if not argv:
            raise QATargetError("no command given")
        program, *rest = argv
        if program != "docker":
            raise QATargetError(
                f"{program} is not available; the only command surface is docker, "
                f"and only against this deployment's containers"
            )
        subcommand = rest[0] if rest else ""
        if subcommand not in CONTAINER_SCOPED_DOCKER:
            raise QATargetError(
                f"docker {subcommand or '(no sub-command)'} is not a read of this deployment; "
                f"allowed: {', '.join(sorted(CONTAINER_SCOPED_DOCKER))}"
            )
        named = [arg for arg in rest[1:] if not arg.startswith("-")]
        if not named:
            raise QATargetError(
                f"docker {subcommand} must name a container of this deployment; "
                "a call that names none describes the host"
            )
        for arg in named:
            self.check_container(arg)
        return await self._run(argv, timeout=REMOTE_EXEC_TIMEOUT)

    async def container_logs(self, container: str, *, tail: int = 200) -> RemoteResult:
        """Read the tail of one container's log."""
        name = self.check_container(container)
        return await self._run(
            ["docker", "logs", "--tail", str(max(1, min(tail, 2000))), name],
            timeout=REMOTE_EXEC_TIMEOUT,
        )

    async def container_inspect(self, container: str) -> RemoteResult:
        """Read one container's state — status, health, restart count."""
        name = self.check_container(container)
        return await self._run(
            ["docker", "inspect", "--format", "{{json .State}}", name],
            timeout=REMOTE_EXEC_TIMEOUT,
        )

    async def localhost_http_get(self, port: int, path: str) -> RemoteResult:
        """GET a path on a loopback port allocated to this deployment.

        The method is not a parameter. A tool that cannot express a POST cannot
        be talked into one, which is the whole of QA's read-only guarantee on
        the application's own API.
        """
        allowed_port = self.check_port(port)
        if not path.startswith("/") or re.search(r"\s", path):
            raise QATargetError(f"{path!r} is not a request path")
        url = f"http://127.0.0.1:{allowed_port}{path}"
        return await self._run(
            [
                "curl",
                "--silent",
                "--show-error",
                "--get",
                "--max-time",
                str(LOCALHOST_PROBE_TIMEOUT),
                "--write-out",
                f"\\n{STATUS_MARKER}%{{http_code}}>>",
                url,
            ],
            timeout=LOCALHOST_PROBE_TIMEOUT + 10,
        )


def _grant_entry(public_key: str, marker: str) -> str:
    expiry = dt.datetime.now(dt.UTC) + dt.timedelta(seconds=GRANT_LIFETIME_S)
    options = f'restrict,expiry-time="{expiry.strftime("%Y%m%d%H%M")}"'
    return f"{options} {public_key.strip()} {marker}"


async def _connect(server_ip: str, ssh_user: str, key: object) -> asyncssh.SSHClientConnection:
    return await asyncssh.connect(
        server_ip,
        username=ssh_user,
        known_hosts=None,
        client_keys=[key],
    )


def _script(body: str, *args: str) -> str:
    """One shell script with its arguments passed as arguments, never inlined."""
    return " ".join(shlex.quote(part) for part in ["sh", "-c", body, "_", *args])


async def _install_grant(target: QATarget, fleet_key: str, entry: str) -> None:
    """Write the run's public key into the QA account's authorized_keys.

    The connection is the administrative one — writing another account's
    `authorized_keys` needs it — and it is the only thing that connection is used
    for. The account written into is the QA one, so the key the run holds admits
    it to that account and to no other.
    """
    try:
        async with await _connect(target.server_ip, target.ssh_user, _import(fleet_key)) as admin:
            result = await admin.run(
                _script(_INSTALL_GRANT, target.qa_ssh_user, entry), check=False
            )
    except (OSError, asyncssh.Error) as exc:
        raise QAGrantError(
            f"could not reach {target.server_ip} to issue a QA identity: {exc}"
        ) from exc
    detail = (result.stderr or result.stdout or "").strip()[:500]
    # The install script separates "this host has no such identity" from every
    # other way an append can fail, and the difference matters to the caller:
    # one is a fact about the host's provisioning that an administrator has to
    # see, the other is this run's bad luck.
    if result.exit_status in (IDENTITY_ABSENT, IDENTITY_KEYS_ABSENT):
        raise QAIdentityAbsentError(
            f"{target.server_handle} records a QA account but {target.server_ip} has none: "
            f"{detail or f'{target.qa_ssh_user} is not on the target'}"
        )
    if result.exit_status != 0:
        raise QAGrantError(
            f"could not install the run identity into {target.qa_ssh_user}@{target.server_ip}: "
            f"{detail}"
        )


async def revoke_grant(
    *, server_ip: str, ssh_user: str, qa_ssh_user: str, fleet_key: str, marker: str
) -> str | None:
    """Remove one run's key and prove it is gone. Returns residual evidence.

    A revoke that is not read back is a revoke that was hoped for. The count is
    taken after the rewrite, from the file itself, and a non-zero count is
    returned as the residue it is. Removing a marker that was never installed is
    a no-op that reads back zero, which is what makes this safe to repeat — the
    sweep calls it for records that may never have installed anything.
    """
    async with await _connect(server_ip, ssh_user, _import(fleet_key)) as admin:
        check = await admin.run(_script(_REVOKE_GRANT, qa_ssh_user, marker), check=False)
    lines = (check.stdout or "").strip().splitlines()
    count = lines[-1].strip() if lines else ""
    # No answer is not "the key is gone". Only a count read back off the file
    # closes a grant; anything else is residue for the sweep to keep working on.
    if check.exit_status != 0 or not count.isdigit():
        return (
            f"the target did not report whether {marker} is gone: "
            f"{(check.stderr or check.stdout or '').strip()[:300] or 'no answer'}"
        )
    if count != "0":
        return f"{count} authorized_keys line(s) matching {marker} survived revocation"
    return None


def _import(ssh_key: str):
    return asyncssh.import_private_key(ssh_key)


@dataclass
class QAGrantOutcome:
    """Whether the run's identity was proven gone, and what is left if not."""

    marker: str
    revoked: bool = False
    residual: str | None = None


def new_grant_marker() -> str:
    """A marker unique to one run, used to find and remove exactly its key."""
    return f"{GRANT_MARKER_PREFIX}-{uuid.uuid4().hex}"


def new_grant(target: QATarget, marker: str) -> QASshGrant:
    """The record written before anything is installed on the target."""
    return QASshGrant(
        marker=marker,
        server_handle=target.server_handle,
        server_ip=target.server_ip,
        ssh_user=target.ssh_user,
        qa_ssh_user=target.qa_ssh_user,
        state=QASshGrantState.ISSUING,
        issued_at=dt.datetime.now(dt.UTC),
    )


@asynccontextmanager
async def qa_target_grant(
    *,
    target: QATarget,
    fleet_ssh_key: str,
    outcome: QAGrantOutcome,
    journal: QAGrantJournal,
) -> AsyncIterator[QATargetSession]:
    """Mint a one-shot identity for this run, and always take it back.

    The record comes first. `ISSUING` is written before the install is
    attempted, so an append that lands while its answer is lost still leaves
    something that knows a key may be out there; only after the install returns
    does the record say `OPEN`. Nothing here infers "no key was installed" from
    a failure to hear back.

    The fleet key is used twice, by this function only: once to install the
    run's public key and once to remove it. Between those the run holds a key
    that exists nowhere else, cannot forward, cannot allocate a terminal, and
    expires by itself if every cleanup path is lost.

    Revocation runs in `finally` on every exit — success, failure, cancellation —
    on its own connection, because the session's connection may be the thing
    that just died. `RELEASED` is written only after the target has been read
    back; anything else leaves the record for the sweep.
    """
    grant = new_grant(target, outcome.marker)
    await journal.write(grant)

    run_key = asyncssh.generate_private_key("ssh-ed25519")
    public_key = run_key.export_public_key("openssh").decode()
    try:
        await _install_grant(target, fleet_ssh_key, _grant_entry(public_key, outcome.marker))
    except QAGrantError:
        # The install may or may not have landed. The record already says a key
        # might be there, and it stays that way until a readback disagrees.
        outcome.residual = (
            f"QA identity {outcome.marker} on {target.server_ip} was left unresolved: "
            "the install did not confirm"
        )
        raise
    grant = grant.model_copy(update={"state": QASshGrantState.OPEN})
    await journal.write(grant)
    logger.info(
        "qa_target_grant_issued",
        server_ip=target.server_ip,
        ssh_user=target.qa_ssh_user,
        marker=outcome.marker,
    )
    try:
        try:
            conn = await _connect(target.server_ip, target.qa_ssh_user, run_key)
        except (OSError, asyncssh.Error) as exc:
            raise QAGrantError(
                f"the run identity could not connect to {target.server_ip}: {exc}"
            ) from exc
        async with conn:
            capabilities = await resolve_capabilities(conn, target)
            yield QATargetSession(target, conn, capabilities)
    finally:
        try:
            outcome.residual = await revoke_grant(
                server_ip=target.server_ip,
                ssh_user=target.ssh_user,
                qa_ssh_user=target.qa_ssh_user,
                fleet_key=fleet_ssh_key,
                marker=outcome.marker,
            )
        except Exception as exc:  # noqa: BLE001 — the run is already ending
            outcome.residual = f"revocation failed: {exc}"
        outcome.revoked = outcome.residual is None
        await journal.write(
            grant.model_copy(
                update={
                    "state": QASshGrantState.RELEASED if outcome.revoked else QASshGrantState.OPEN,
                    "revoke_attempts": grant.revoke_attempts + 1,
                    "detail": outcome.residual,
                }
            )
        )
        logger.info(
            "qa_target_grant_revoked",
            server_ip=target.server_ip,
            marker=outcome.marker,
            revoked=outcome.revoked,
            residual=outcome.residual,
        )
