"""The one target a central QA run can reach, and the identity it reaches it with.

QA used to be an agent living on the deploy target with the fleet's own server
key in its hands. It is central now, and this module is the whole of what a run
can do to the machine it is testing:

* **a capability set.** A run has one, resolved once from deployment data before
  any tool exists: the physical root of its deployment directory (resolved on
  the target, so a symlink cannot widen it), the containers of its compose
  project as docker itself reports them, the loopback ports allocated to its
  application, and its public URL. Every tool derives its boundary from that set
  and from nothing else. A tool whose boundary cannot be derived from it does
  not exist here — that is why there is no `docker ps`, no `docker images` and
  no host diagnostics: those enumerate the machine, not the deployment.
* **a one-shot SSH key into an account it does not own.** The account is
  `QATarget.qa_ssh_user`: an unprivileged account provisioning created on the
  target for exactly this, recorded on the server row, and never the
  administrative account the fleet key opens. The runner's whole power over it
  is to append one `restrict`ed, expiring key to the `authorized_keys` the
  provisioning role opened, and to take that key back out; it creates no
  account, no directory and no file, so a target that was not provisioned for QA
  refuses the install instead of being made to admit a run. The agent holds
  neither key. That a key may be out there is written down before it is
  installed (`QASshGrant`), so an ambiguous failure leaves a record the sweep can
  act on.
* **a closed set of typed operations.** There is no "run this shell command"
  here: every method below names what it does, checks its arguments against the
  capability set, and refuses anything else. The write guard is the absence of a
  write, not a filter over one.
"""

from __future__ import annotations

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

logger = structlog.get_logger(__name__)

SERVICE_BASE_DIR = "/opt/services"
GRANT_MARKER_PREFIX = "codegen-qa-run"
# An sshd-side backstop for the case both the runner and the sweep are lost.
# The runner's `finally` is the primary removal and the sweep is the second one;
# this is what stops a forgotten key from outliving the machine.
GRANT_LIFETIME_S = 3600
REMOTE_EXEC_TIMEOUT = 60
LOCALHOST_PROBE_TIMEOUT = 30
MAX_REMOTE_OUTPUT = 8000
MAX_PORT = 65535
STATUS_MARKER = "<<qa-http-status:"
# Exit statuses `_CONTAINED_READ` answers with, so the caller can tell a refusal
# from a read that simply found nothing.
READ_UNRESOLVABLE = 3
READ_OUTSIDE_ROOT = 4
# Compose stamps this on every container it creates, and it is the deployment's
# own name for its containers — not a naming convention this code assumes.
COMPOSE_PROJECT_LABEL = "com.docker.compose.project"

# How the QA account reaches docker. It is not in the `docker` group — that
# group is root on the host — and cannot open the socket, so every docker call
# goes through the wrapper provisioning installed, which refuses on the target
# every sub-command that writes or escapes. The membership test below still
# decides *which* containers a run may name; this decides what may be done to
# any of them, and it is enforced by the machine rather than by the caller.
QA_DOCKER_WRAPPER = "/usr/local/bin/qa-docker"
QA_DOCKER = ("sudo", "-n", QA_DOCKER_WRAPPER)

# Read-only docker sub-commands that name a container. Each one is bounded by
# the run's container capability: the container it names must be in the set.
# Sub-commands that name no container (ps, images, version, a bare stats) are
# absent on purpose — their answer is about the host, and no element of the
# capability set can bound them.
CONTAINER_SCOPED_DOCKER = frozenset({"diff", "inspect", "logs", "port", "stats", "top"})

# Reading the application's own files is in scope; reading the credentials the
# deploy put next to them is not, and QA has no test that needs them. This sits
# on top of physical containment, not instead of it.
SECRET_FILE_PATTERNS = (
    re.compile(r"(^|/)\.env(\.|$)"),
    re.compile(r"\.(pem|key|p12|pfx)$"),
    re.compile(r"(^|/)(credentials|secrets)[^/]*$", re.IGNORECASE),
    re.compile(r"(^|/)\.ssh(/|$)"),
)

# Resolve the path on the target and read it only if what it resolves to is
# inside the physical root. The path arrives as $1 and the root as $2, so
# nothing the agent names is ever interpolated into this text.
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

# Append one run key to an account that already exists, and to nothing else.
# The account name arrives as $1 and the entry as $2, so nothing is interpolated
# into this text. Every path out that is not "the key was appended" is an error:
# an absent account or an absent `authorized_keys` means this target was never
# provisioned for QA, and creating either here would be the runtime minting
# itself access instead of borrowing what provisioning laid out.
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

# Remove one run's key from that account and print how many of its lines are
# still there. An account or a file that is not there holds no key, so both
# answer zero rather than failing: this runs for records that may never have
# installed anything.
#
# The rewrite refuses to copy an empty filter result over the file. The QA
# account's `authorized_keys` is opened by provisioning with a comment line that
# is never a key and never carries a run marker, so a filter that kept nothing
# did not find only our key — it failed, and copying that would leave a file the
# next run cannot append to under a lock it cannot take. Leaving it alone makes
# the readback report the marker as still there, which is the residue the sweep
# is for.
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


class QACapabilityError(RuntimeError):
    """The run's capability set could not be resolved from the target."""


@dataclass(frozen=True)
class QATarget:
    """Where the run's one deployment lives, and how to address it."""

    server_ip: str
    # The administrative account the fleet key opens — `root` on a server row
    # `server_sync` created. It is used twice per run, by the runner only, to put
    # the run's key into the QA account and to take it out. No run is ever
    # performed as this account.
    ssh_user: str
    # The unprivileged account provisioning made for QA runs on this host, taken
    # from the server row. This is who the run is.
    qa_ssh_user: str
    server_handle: str
    project_name: str
    deployed_url: str
    # Ports the platform allocated to this application. This is the deployment
    # data the loopback capability is built from; nothing is guessed from what
    # happens to be listening.
    allocated_ports: frozenset[int] = frozenset()
    bot_username: str | None = None

    @property
    def service_dir(self) -> str:
        return f"{SERVICE_BASE_DIR}/{self.project_name}"


@dataclass(frozen=True)
class QACapabilities:
    """Everything this run may see, and the only source of any tool's boundary."""

    deployed_url: str
    # The deployment directory as the target itself resolves it. Containment is
    # checked against this, after resolution, so a symlink inside the tree
    # cannot point out of it and stay "inside" by spelling.
    physical_root: str
    containers: frozenset[str]
    loopback_ports: frozenset[int]
    # The one bot this deployment answers as. The Telegram tool addresses this
    # and nothing else, so its boundary comes from here like every other tool's.
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
    """How this argv is actually spelled on the target.

    Everything but docker runs as the QA account itself. Docker runs through the
    wrapper, because the account has no other way to reach the daemon — and that
    is the point: the target, not this process, is what refuses `docker exec`.
    """
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
    """Ask the target what this run's deployment actually is.

    Two of the three elements can only be answered by the target: what the
    deployment directory resolves to physically, and which containers docker
    considers part of this compose project. Both are read once, with the run's
    own identity, before any tool exists — so every later check is a membership
    test against a fixed set rather than a rule a tool invented.
    """
    root = await conn.run(f"readlink -f -- {shlex.quote(target.service_dir)}", check=False)
    physical_root = (root.stdout or "").strip()
    if root.exit_status != 0 or not physical_root:
        raise QACapabilityError(
            f"{target.service_dir} does not resolve on {target.server_ip}: "
            f"{(root.stderr or '').strip()[:300] or 'no such directory'}"
        )

    listing = await conn.run(
        " ".join(
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
        ),
        check=False,
    )
    if listing.exit_status != 0:
        raise QACapabilityError(
            f"cannot list the containers of {target.project_name} on {target.server_ip}: "
            f"{(listing.stderr or listing.stdout or '').strip()[:300]}"
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
    if result.exit_status != 0:
        raise QAGrantError(
            f"could not install the run identity into {target.qa_ssh_user}@{target.server_ip}: "
            f"{(result.stderr or result.stdout or '').strip()[:500]}"
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
