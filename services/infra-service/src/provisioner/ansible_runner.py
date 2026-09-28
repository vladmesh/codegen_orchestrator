"""Ansible playbook execution for provisioner."""

import json
import os
from pathlib import Path
import subprocess
import tempfile
import time

import structlog

from shared.diagnostics import redact_diagnostic

from ..config.constants import Paths, Timeouts

logger = structlog.get_logger()

MAX_LOG_LENGTH = 1000

# The recap block ansible prints last, and enough of the output before it to
# carry the play's own closing statements. A successful provisioning used to
# leave nothing behind but a debug line truncated to its first 1000 characters,
# so the one question an acceptance artifact could not answer was whether a role
# had run at all. Both are bounded, and both are redacted before they are logged.
PLAY_RECAP_MARKER = "PLAY RECAP"
PLAY_TAIL_LENGTH = 3000

# The play's closing statement about the QA seat this host lends, kept as its
# own field instead of being left to the tail's luck. Run 33729987635 pushed it
# out of the last 3000 characters — the roles after it print more than that —
# so the artifact of a provisioning that *did* prove the seat still could not
# show the proof. A marker and a bounded window, cut from the same redacted
# output as everything else here.
QA_IDENTITY_MARKER = "qa_identity_proof"
QA_IDENTITY_REPORT_LENGTH = 1000

# Configuration from centralized constants
PROVISIONING_TIMEOUT = Timeouts.PROVISIONING
REINSTALL_TIMEOUT = Timeouts.REINSTALL


def _redact_private_key(value: str, private_key: str | None) -> str:
    """Prevent a supplied SSH private key from reaching logs or callers."""
    if private_key:
        return value.replace(private_key, "[REDACTED SSH PRIVATE KEY]")
    return value


def _play_recap(stdout: str) -> str:
    """The play's own account of what ran on the host, or the fact that it has none.

    Kept for every outcome, not only failure: "which roles ran on the target
    this run recorded complete" is a question about a *successful* provisioning,
    and the run that raised it had no answer to read.
    """
    index = stdout.rfind(PLAY_RECAP_MARKER)
    if index < 0:
        return "no PLAY RECAP: this run produced no recap of its own play"
    return stdout[index:].strip()


def _qa_identity_report(stdout: str) -> str:
    """What the play said about the QA seat, or the fact that it said nothing.

    Both plays that create the seat report it under this one key —
    `provision_software.yml` and `qa_identity_retrofit.yml` — so one marker
    finds either. Taken from the last mention because the report task is the
    play's own last word on the role, and the role's own tasks name the
    variable earlier in the output.
    """
    index = stdout.rfind(QA_IDENTITY_MARKER)
    if index < 0:
        return f"no {QA_IDENTITY_MARKER}: this play reported no QA identity"
    return stdout[index : index + QA_IDENTITY_REPORT_LENGTH].strip()


class AnsibleRunner:
    """Executes Ansible playbooks."""

    def run_playbook(  # noqa: PLR0913, PLR0915
        self,
        *,
        server_ip: str,
        server_handle: str,
        playbook_name: str,
        root_password: str | None = None,
        ssh_public_key: str | None = None,
        deploy_user: str | None = None,
        ssh_user: str | None = None,
        ssh_private_key: str | None = None,
        orchestrator_ip: str | None = None,
        orchestrator_hostname: str | None = None,
        tags: list[str] | None = None,
        timeout: int = 600,
        extra_vars: dict[str, str] | None = None,
        secret_values: tuple[str, ...] = (),
    ) -> tuple[bool, str]:
        """Run an Ansible playbook.

        Args:
            server_ip: Server IP address
            server_handle: Server handle for hostname
            playbook_name: Name of playbook file (e.g., 'provision_access.yml')
            root_password: Optional root password (if None, uses SSH key auth)
            ssh_public_key: Optional SSH public key to inject
            deploy_user: SSH user that receives deploy-target access
            ssh_user: SSH user for key-authenticated connections
            ssh_private_key: Private key for key-authenticated connections
            orchestrator_ip: Optional orchestrator public IP for UFW rules
            orchestrator_hostname: Optional orchestrator hostname for Loki push URL
            tags: Optional Ansible tags, for applying one supported baseline component
            timeout: Execution timeout in seconds
            extra_vars: Additional playbook variables, for playbooks whose inputs
                are their own rather than part of the provisioning vocabulary
            secret_values: Every additional resolved secret that could occur in output

        Returns:
            Tuple of (success: bool, output: str)
        """
        secrets = (
            *secret_values,
            root_password or "",
            ssh_private_key or "",
            (extra_vars or {}).get("github_token", ""),
        )

        def safe(value: object) -> str:
            return redact_diagnostic(
                _redact_private_key(str(value), ssh_private_key), secrets=secrets
            )

        try:
            playbook_path = Paths.playbook(playbook_name)
            ansible_config_path = Path(playbook_path).parent.parent / "ansible.cfg"
            if not ansible_config_path.is_file():
                message = safe(f"Ansible configuration not found: {ansible_config_path}")
                logger.error("ansible_config_missing", error=message)
                return False, message
            if not root_password and bool(ssh_user) != bool(ssh_private_key):
                return False, "SSH key authentication requires both SSH user and private key"

            # This private directory is outside checkouts even if TMPDIR points at
            # a product. Its context also cleans partial setup and execution failures.
            with tempfile.TemporaryDirectory(prefix="codegen-ansible-", dir="/tmp") as directory:
                private_dir = Path(directory)

                def write_private(name: str, content: str) -> Path:
                    path = private_dir / name
                    # Apply the mode at creation, before any secret bytes are written.
                    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                    with os.fdopen(fd, "w") as stream:
                        stream.write(content)
                    return path

                ssh_args = "ansible_ssh_common_args='-o UserKnownHostsFile=/dev/null'"
                if root_password:
                    inventory_content = (
                        f"[target]\n{server_ip} ansible_user=root "
                        f"ansible_ssh_pass={root_password} {ssh_args}\n"
                    )
                elif ssh_private_key and ssh_user:
                    key_path = write_private("ssh.key", ssh_private_key.rstrip("\r\n") + "\n")
                    inventory_content = (
                        f"[target]\n{server_ip} ansible_user={ssh_user} "
                        f"ansible_ssh_private_key_file={key_path} {ssh_args}\n"
                    )
                else:
                    inventory_content = f"[target]\n{server_ip} ansible_user=root {ssh_args}\n"
                inventory_path = write_private("inventory.ini", inventory_content)
                variables = {"target_host": server_ip, "server_hostname": server_handle}
                for name, value in (
                    ("ssh_public_key", ssh_public_key),
                    ("deploy_user", deploy_user),
                    ("orchestrator_ip", orchestrator_ip),
                    ("orchestrator_hostname", orchestrator_hostname),
                ):
                    if value:
                        variables[name] = value
                variables.update(extra_vars or {})
                vars_path = write_private("vars.json", json.dumps(variables))
                cmd = [
                    "ansible-playbook",
                    "-i",
                    str(inventory_path),
                    playbook_path,
                    "--extra-vars",
                    f"@{vars_path}",
                    "-v",
                ]
                if tags:
                    cmd.extend(["--tags", ",".join(tags)])
                logger.info(
                    "ansible_playbook_start",
                    playbook=safe(playbook_name),
                    server_handle=safe(server_handle),
                    server_ip=safe(server_ip),
                    auth_mode="password" if root_password else "key",
                )
                start = time.time()
                process = subprocess.run(  # noqa: S603
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                    env={**os.environ, "ANSIBLE_CONFIG": str(ansible_config_path)},
                )

            # Log output (abbreviated)
            stdout = safe(process.stdout)
            stderr = safe(process.stderr)
            stdout_brief = (
                stdout[:MAX_LOG_LENGTH] + "..." if len(stdout) > MAX_LOG_LENGTH else stdout
            )
            logger.debug("ansible_stdout", output=stdout_brief)

            if stderr:
                logger.warning("ansible_stderr", output=stderr[:MAX_LOG_LENGTH])

            success = process.returncode == 0
            duration = time.time() - start
            logger.info(
                "ansible_playbook_complete",
                playbook=safe(playbook_name),
                server_handle=safe(server_handle),
                exit_code=process.returncode,
                duration_sec=round(duration, 2),
            )
            # The play's own last words, at a level a service log tail keeps.
            # The recap is which hosts the play touched and how it ended on
            # each; the tail is the closing output around it; and the QA
            # identity report is cut out by name rather than left to fall
            # inside a fixed number of trailing characters, because it did not.
            logger.info(
                "ansible_play_recap",
                playbook=safe(playbook_name),
                server_handle=safe(server_handle),
                recap=_play_recap(stdout)[-PLAY_TAIL_LENGTH:],
                tail=stdout[-PLAY_TAIL_LENGTH:],
                qa_identity=_qa_identity_report(stdout),
            )
            if success:
                output = stdout
            else:
                # On failure, capture stderr and the LAST 1000 chars of stdout
                stdout_tail = stdout[-MAX_LOG_LENGTH:] if len(stdout) > MAX_LOG_LENGTH else stdout
                output = f"STDERR: {stderr[-MAX_LOG_LENGTH:]}\n\nSTDOUT TAIL:\n{stdout_tail}"
                # Log failure details for easier troubleshooting
                logger.error(
                    "ansible_playbook_failed",
                    playbook=safe(playbook_name),
                    server_handle=safe(server_handle),
                    exit_code=process.returncode,
                    stderr=stderr[:MAX_LOG_LENGTH] if stderr else None,
                    stdout_tail=stdout_tail,
                )

            return success, output

        except subprocess.TimeoutExpired:
            logger.error("ansible_playbook_timeout", playbook=safe(playbook_name), timeout=timeout)
            return False, f"Timeout after {timeout}s"
        except Exception as e:
            logger.error(
                "ansible_playbook_exception",
                playbook=safe(playbook_name),
                error=safe(e)[:MAX_LOG_LENGTH],
                error_type=type(e).__name__,
            )
            return False, f"{type(e).__name__}: {safe(e)[:MAX_LOG_LENGTH]}"
