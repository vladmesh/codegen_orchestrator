"""The one target a central QA run can reach, and the identity it reaches it with.

QA used to be an agent living on the deploy target with the fleet's own server
key in its hands. It is central now, and this module is the whole of what a run
can do to the machine it is testing:

* a one-shot SSH identity, minted per run and removed when the run ends. It is
  not the fleet key and it is not root — the fleet key is used once, by the
  runner, to write the run's own public key into the target's ``authorized_keys``
  and once more to take it back out. The agent never holds either key.
* a closed set of typed operations. There is no "run this shell command" here:
  every method below names what it does, validates its arguments, and refuses
  anything that is not a read. The write guard is the absence of a write, not a
  filter over one.
* exactly one target. A session is constructed from one `QATarget` and every
  path, container and port it accepts is checked against that target, so an
  agent that names another project's container is refused rather than answered.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
import datetime as dt
import re
import shlex
import uuid

import asyncssh
import structlog

logger = structlog.get_logger(__name__)

SERVICE_BASE_DIR = "/opt/services"
GRANT_MARKER_PREFIX = "codegen-qa-run"
# An sshd-side backstop for the case the runner dies between grant and revoke.
# The sweep the runner performs in its `finally` is the primary removal; this is
# what stops a lost key from outliving the machine.
GRANT_LIFETIME_S = 3600
GRANT_LOCK = "$HOME/.ssh/.codegen-qa.lock"
AUTHORIZED_KEYS = "$HOME/.ssh/authorized_keys"
REMOTE_EXEC_TIMEOUT = 60
LOCALHOST_PROBE_TIMEOUT = 30
MAX_REMOTE_OUTPUT = 8000
STATUS_MARKER = "<<qa-http-status:"
MAX_PORT = 65535

# Programs the run identity may execute, and the sub-commands allowed for the
# ones whose first argument decides whether the call reads or writes. Anything
# absent is refused: an allowlist is the only form of this check that stays
# correct when a new tool is added to the target.
#
# Two kinds of thing are deliberately absent. File readers, because `read_file`
# already reads files and is scoped to the deployment directory, so a general
# `cat` would be a second and wider door to the same place. And anything that
# names a container or a compose project — `docker logs`, `docker inspect`,
# `docker compose ...` — because the dedicated tools for those check the name
# against this run's deployment, and reaching them through `exec` would skip
# that check and let one run read another project's logs.
ALLOWED_REMOTE_COMMANDS: dict[str, frozenset[str] | None] = {
    "date": None,
    "df": None,
    "docker": frozenset({"ps", "images", "stats", "version"}),
    "free": None,
    "uname": None,
    "uptime": None,
}
# Reading the application's own files is in scope; reading the credentials the
# deploy put next to them is not, and QA has no test that needs them.
SECRET_FILE_PATTERNS = (
    re.compile(r"(^|/)\.env(\.|$)"),
    re.compile(r"\.(pem|key|p12|pfx)$"),
    re.compile(r"(^|/)(credentials|secrets)[^/]*$", re.IGNORECASE),
    re.compile(r"(^|/)\.ssh(/|$)"),
)


class QATargetError(RuntimeError):
    """A QA tool was asked for something outside the run's target or contract."""


class QAGrantError(RuntimeError):
    """The one-shot identity for this run could not be issued."""


@dataclass(frozen=True)
class QATarget:
    """Everything a run is allowed to know about the machine it tests."""

    server_ip: str
    ssh_user: str
    project_name: str
    deployed_url: str
    bot_username: str | None = None

    @property
    def service_dir(self) -> str:
        return f"{SERVICE_BASE_DIR}/{self.project_name}"


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


def _reject_secret_path(path: str) -> None:
    for pattern in SECRET_FILE_PATTERNS:
        if pattern.search(path):
            raise QATargetError(
                f"{path} holds deployment credentials; QA tests the running application, "
                "not the secrets it was deployed with"
            )


class QATargetSession:
    """The typed surface a QA run has on its single target."""

    def __init__(self, target: QATarget, conn: asyncssh.SSHClientConnection) -> None:
        self._target = target
        self._conn = conn

    @property
    def target(self) -> QATarget:
        return self._target

    def resolve_path(self, path: str) -> str:
        """Return an absolute path inside this target's service directory.

        A relative path is taken from the service directory. An absolute path
        outside it is refused: the run is scoped to one deployed project, and a
        tool that answers about `/etc` or another project's directory has left
        that scope.
        """
        service_dir = self._target.service_dir
        candidate = path if path.startswith("/") else f"{service_dir}/{path}"
        normalized = re.sub(r"/{2,}", "/", candidate).rstrip("/") or "/"
        if ".." in normalized.split("/"):
            raise QATargetError(f"{path} escapes {service_dir}")
        if normalized != service_dir and not normalized.startswith(f"{service_dir}/"):
            raise QATargetError(f"{path} is outside this run's target directory {service_dir}")
        _reject_secret_path(normalized)
        return normalized

    def resolve_container(self, container: str) -> str:
        """Return a container name that belongs to this run's deployment.

        Compose names containers `<project>-<service>-<n>`, so the project
        prefix is what proves a container belongs to the target this run owns.
        """
        if not re.fullmatch(r"[A-Za-z0-9._-]+", container):
            raise QATargetError(f"{container!r} is not a container name")
        prefix = f"{self._target.project_name}-"
        if container != self._target.project_name and not container.startswith(prefix):
            raise QATargetError(
                f"{container} does not belong to {self._target.project_name}; this run can only "
                "inspect its own deployment"
            )
        return container

    async def _run(self, argv: list[str], *, timeout: int) -> RemoteResult:
        command = " ".join(shlex.quote(part) for part in argv)
        result = await self._conn.run(command, check=False, timeout=timeout)
        return RemoteResult(
            exit_status=result.exit_status if result.exit_status is not None else -1,
            stdout=result.stdout or "",
            stderr=result.stderr or "",
        )

    async def read_file(self, path: str, *, max_bytes: int = MAX_REMOTE_OUTPUT) -> RemoteResult:
        """Read one file from the target's service directory."""
        resolved = self.resolve_path(path)
        return await self._run(
            ["head", "-c", str(max_bytes), resolved], timeout=REMOTE_EXEC_TIMEOUT
        )

    async def exec(self, argv: list[str]) -> RemoteResult:
        """Run one allowlisted, non-interactive command as the run identity."""
        if not argv:
            raise QATargetError("no command given")
        program, *rest = argv
        if program not in ALLOWED_REMOTE_COMMANDS:
            allowed = ", ".join(sorted(ALLOWED_REMOTE_COMMANDS))
            raise QATargetError(f"{program} is not a QA-readable command; allowed: {allowed}")
        allowed_subcommands = ALLOWED_REMOTE_COMMANDS[program]
        if allowed_subcommands is not None:
            # The sub-command has to be the very first argument. Scanning for
            # the first non-flag token instead would let a global flag's value
            # pose as the sub-command — `docker --context ps rm x` reads as
            # "ps" and runs `rm`. A leading flag is refused rather than parsed.
            subcommand = rest[0] if rest else ""
            if subcommand not in allowed_subcommands:
                raise QATargetError(
                    f"{program} {subcommand or '(no sub-command)'} is not read-only; "
                    f"allowed: {', '.join(sorted(allowed_subcommands))}"
                )
        return await self._run(argv, timeout=REMOTE_EXEC_TIMEOUT)

    async def container_logs(self, container: str, *, tail: int = 200) -> RemoteResult:
        """Read the tail of one container's log."""
        name = self.resolve_container(container)
        return await self._run(
            ["docker", "logs", "--tail", str(max(1, min(tail, 2000))), name],
            timeout=REMOTE_EXEC_TIMEOUT,
        )

    async def container_inspect(self, container: str) -> RemoteResult:
        """Read one container's state — status, health, restart count."""
        name = self.resolve_container(container)
        return await self._run(
            [
                "docker",
                "inspect",
                "--format",
                "{{json .State}}",
                name,
            ],
            timeout=REMOTE_EXEC_TIMEOUT,
        )

    async def localhost_http_get(self, port: int, path: str) -> RemoteResult:
        """GET a path on the target's loopback interface.

        The method is not a parameter. A tool that cannot express a POST cannot
        be talked into one, which is the whole of QA's read-only guarantee on
        the application's own API.
        """
        if not 1 <= port <= MAX_PORT:
            raise QATargetError(f"{port} is not a port")
        if not path.startswith("/") or re.search(r"\s", path):
            raise QATargetError(f"{path!r} is not a request path")
        url = f"http://127.0.0.1:{port}{path}"
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


async def _install_grant(target: QATarget, fleet_key: str, entry: str) -> None:
    """Write the run's public key into the target's authorized_keys."""
    append = f"printf '%s\\n' {shlex.quote(entry)} >> {AUTHORIZED_KEYS}"
    install = (
        f"mkdir -p $HOME/.ssh && chmod 700 $HOME/.ssh && touch {AUTHORIZED_KEYS} && "
        f"chmod 600 {AUTHORIZED_KEYS} && flock {GRANT_LOCK} -c {shlex.quote(append)}"
    )
    try:
        async with await _connect(target.server_ip, target.ssh_user, _import(fleet_key)) as admin:
            result = await admin.run(install, check=False)
    except (OSError, asyncssh.Error) as exc:
        raise QAGrantError(
            f"could not reach {target.server_ip} to issue a QA identity: {exc}"
        ) from exc
    if result.exit_status != 0:
        raise QAGrantError(
            f"could not install the run identity on {target.server_ip}: "
            f"{(result.stderr or result.stdout or '').strip()[:500]}"
        )


async def _revoke_grant(target: QATarget, fleet_key: str, marker: str) -> str | None:
    """Remove the run's key and prove it is gone. Returns residual evidence.

    A revoke that is not read back is a revoke that was hoped for. The count is
    taken after the rewrite, from the file itself, and a non-zero count is
    returned as the residue it is.
    """
    remove = shlex.quote(
        f"grep -v -F {shlex.quote(marker)} {AUTHORIZED_KEYS} > {AUTHORIZED_KEYS}.qa-tmp || true; "
        f"cat {AUTHORIZED_KEYS}.qa-tmp > {AUTHORIZED_KEYS}; rm -f {AUTHORIZED_KEYS}.qa-tmp"
    )
    verify = f"grep -c -F {shlex.quote(marker)} {AUTHORIZED_KEYS} || true"
    async with await _connect(target.server_ip, target.ssh_user, _import(fleet_key)) as admin:
        await admin.run(f"flock {GRANT_LOCK} -c {remove}", check=False)
        check = await admin.run(verify, check=False)
    remaining = (check.stdout or "").strip()
    if remaining and remaining != "0":
        return f"{remaining} authorized_keys line(s) matching {marker} survived revocation"
    return None


def _import(ssh_key: str):
    return asyncssh.import_private_key(ssh_key)


@dataclass
class QAGrantOutcome:
    """Whether the run's identity was proven gone, and what is left if not."""

    marker: str
    revoked: bool = False
    residual: str | None = None


@asynccontextmanager
async def qa_target_grant(
    *, target: QATarget, fleet_ssh_key: str, outcome: QAGrantOutcome
) -> AsyncIterator[QATargetSession]:
    """Mint a one-shot identity for this run, and always take it back.

    The fleet key is used twice, by this function only: once to install the
    run's public key and once to remove it. Between those two the run holds a
    key that exists nowhere else, cannot forward, cannot allocate a terminal and
    expires by itself if this process never reaches its own cleanup.

    Revocation runs in `finally` on every exit — success, failure, cancellation —
    and on its own connection, because the session's connection may be the thing
    that just died.
    """
    run_key = asyncssh.generate_private_key("ssh-ed25519")
    public_key = run_key.export_public_key("openssh").decode()
    await _install_grant(target, fleet_ssh_key, _grant_entry(public_key, outcome.marker))
    logger.info(
        "qa_target_grant_issued",
        server_ip=target.server_ip,
        ssh_user=target.ssh_user,
        marker=outcome.marker,
    )
    try:
        try:
            conn = await _connect(target.server_ip, target.ssh_user, run_key)
        except (OSError, asyncssh.Error) as exc:
            raise QAGrantError(
                f"the run identity could not connect to {target.server_ip}: {exc}"
            ) from exc
        async with conn:
            yield QATargetSession(target, conn)
    finally:
        try:
            outcome.residual = await _revoke_grant(target, fleet_ssh_key, outcome.marker)
            outcome.revoked = outcome.residual is None
        except Exception as exc:  # noqa: BLE001 — the run is already ending
            outcome.residual = f"revocation failed: {exc}"
            outcome.revoked = False
        logger.info(
            "qa_target_grant_revoked",
            server_ip=target.server_ip,
            marker=outcome.marker,
            revoked=outcome.revoked,
            residual=outcome.residual,
        )


def new_grant_marker() -> str:
    """A marker unique to one run, used to find and remove exactly its key."""
    return f"{GRANT_MARKER_PREFIX}-{uuid.uuid4().hex}"
