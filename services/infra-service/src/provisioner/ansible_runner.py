"""Ansible playbook execution for provisioner."""

import os
from pathlib import Path
import shlex
import subprocess
import tempfile
import time

import structlog

from ..config.constants import Paths, Timeouts

logger = structlog.get_logger()

MAX_LOG_LENGTH = 1000

# Configuration from centralized constants
PROVISIONING_TIMEOUT = Timeouts.PROVISIONING
REINSTALL_TIMEOUT = Timeouts.REINSTALL


def _redact_private_key(value: str, private_key: str | None) -> str:
    """Prevent a supplied SSH private key from reaching logs or callers."""
    if private_key:
        return value.replace(private_key, "[REDACTED SSH PRIVATE KEY]")
    return value


class AnsibleRunner:
    """Executes Ansible playbooks."""

    def run_playbook(  # noqa: PLR0913, PLR0915
        self,
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

        Returns:
            Tuple of (success: bool, output: str)
        """
        playbook_path = Paths.playbook(playbook_name)
        ansible_config_path = Path(playbook_path).parent.parent / "ansible.cfg"
        if not ansible_config_path.is_file():
            message = f"Ansible configuration not found: {ansible_config_path}"
            logger.error("ansible_config_missing", config_path=str(ansible_config_path))
            return False, message

        # Inventory construction
        # host_key_checking disables only StrictHostKeyChecking. Keep known_hosts
        # unwritten so a reinstall at the same IP can still use password auth.
        ssh_args = "ansible_ssh_common_args='-o UserKnownHostsFile=/dev/null'"
        private_key_path: str | None = None
        if root_password:
            # Password authentication
            inventory_content = f"""[target]
{server_ip} ansible_user=root ansible_ssh_pass={root_password} {ssh_args}
"""

        else:
            if bool(ssh_user) != bool(ssh_private_key):
                return False, "SSH key authentication requires both SSH user and private key"
            if ssh_private_key and ssh_user:
                normalized_private_key = ssh_private_key.rstrip("\r\n") + "\n"
                with tempfile.NamedTemporaryFile(mode="w", delete=False, suffix=".key") as key_file:
                    key_file.write(normalized_private_key)
                    private_key_path = key_file.name
                os.chmod(private_key_path, 0o600)
                inventory_content = f"""[target]
{server_ip} ansible_user={ssh_user} ansible_ssh_private_key_file={private_key_path} {ssh_args}
"""
            else:
                inventory_content = f"""[target]
{server_ip} ansible_user=root {ssh_args}
"""

        with tempfile.NamedTemporaryFile(mode="w", delete=False, suffix=".ini") as inv_file:
            inv_file.write(inventory_content)
            inventory_path = inv_file.name

        # Extra vars for playbook
        vars_arg = f"target_host={server_ip} server_hostname={server_handle}"

        if ssh_public_key:
            vars_arg += f" ssh_public_key='{ssh_public_key}'"

        if deploy_user:
            vars_arg += f" deploy_user={deploy_user}"

        if orchestrator_ip:
            vars_arg += f" orchestrator_ip={orchestrator_ip}"

        if orchestrator_hostname:
            vars_arg += f" orchestrator_hostname={orchestrator_hostname}"

        for key, value in (extra_vars or {}).items():
            vars_arg += f" {key}={shlex.quote(value)}"

        # Construct ansible-playbook command
        cmd = [
            "ansible-playbook",
            "-i",
            # This per-run inventory deliberately overrides the production inventory
            # from ansible.cfg with the server selected by the provisioner.
            inventory_path,
            playbook_path,
            "--extra-vars",
            vars_arg,
            "-v",
        ]
        if tags:
            cmd.extend(["--tags", ",".join(tags)])

        auth_mode = "password" if root_password else "key"
        logger.info(
            "ansible_playbook_start",
            playbook=playbook_name,
            server_handle=server_handle,
            server_ip=server_ip,
            auth_mode=auth_mode,
        )

        start = time.time()

        try:
            # Roles, host-key policy, and privilege escalation come from this
            # repository config instead of the process working directory.
            process = subprocess.run(  # noqa: S603
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
                env={**os.environ, "ANSIBLE_CONFIG": str(ansible_config_path)},
            )

            # Log output (abbreviated)
            stdout = _redact_private_key(process.stdout, ssh_private_key)
            stderr = _redact_private_key(process.stderr, ssh_private_key)
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
                playbook=playbook_name,
                server_handle=server_handle,
                exit_code=process.returncode,
                duration_sec=round(duration, 2),
            )
            if success:
                output = stdout
            else:
                # On failure, capture stderr and the LAST 1000 chars of stdout
                stdout_tail = stdout[-MAX_LOG_LENGTH:] if len(stdout) > MAX_LOG_LENGTH else stdout
                output = f"STDERR: {stderr}\n\nSTDOUT TAIL:\n{stdout_tail}"
                # Log failure details for easier troubleshooting
                logger.error(
                    "ansible_playbook_failed",
                    playbook=playbook_name,
                    server_handle=server_handle,
                    exit_code=process.returncode,
                    stderr=stderr[:MAX_LOG_LENGTH] if stderr else None,
                    stdout_tail=stdout_tail,
                )

            return success, output

        except subprocess.TimeoutExpired:
            logger.error("ansible_playbook_timeout", playbook=playbook_name, timeout=timeout)
            return False, f"Timeout after {timeout}s"
        except Exception as e:
            logger.error(
                "ansible_playbook_exception",
                playbook=playbook_name,
                error=_redact_private_key(str(e), ssh_private_key),
                error_type=type(e).__name__,
                exc_info=True,
            )
            return False, _redact_private_key(str(e), ssh_private_key)
        finally:
            # Cleanup
            if os.path.exists(inventory_path):
                os.remove(inventory_path)
            if private_key_path and os.path.exists(private_key_path):
                os.remove(private_key_path)
