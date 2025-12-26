"""Ansible playbook execution for provisioner."""

import os
import time
import subprocess
import tempfile

import structlog

logger = structlog.get_logger()

MAX_LOG_LENGTH = 1000

# Configuration from environment
PROVISIONING_TIMEOUT = int(os.getenv("PROVISIONING_TIMEOUT", "1200"))  # 20 minutes
REINSTALL_TIMEOUT = int(os.getenv("REINSTALL_TIMEOUT", "900"))  # 15 minutes


def run_ansible_playbook(
    server_ip: str,
    server_handle: str,
    playbook_name: str,
    root_password: str | None = None,
    ssh_public_key: str | None = None,
    timeout: int = 600,
) -> tuple[bool, str]:
    """Run an Ansible playbook.

    Args:
        server_ip: Server IP address
        server_handle: Server handle for hostname
        playbook_name: Name of playbook file (e.g., 'provision_access.yml')
        root_password: Optional root password (if None, uses SSH key auth)
        ssh_public_key: Optional SSH public key to inject
        timeout: Execution timeout in seconds

    Returns:
        Tuple of (success: bool, output: str)
    """
    playbook_path = f"/app/services/infrastructure/ansible/playbooks/{playbook_name}"

    # Inventory construction
    ssh_args = (
        "ansible_ssh_common_args='-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null'"
    )
    if root_password:
        # Password authentication
        inventory_content = f"""[target]
{server_ip} ansible_user=root ansible_ssh_pass={root_password} {ssh_args}
"""

    else:
        # Key authentication (uses default SSH key from ~/.ssh)
        inventory_content = f"""[target]
{server_ip} ansible_user=root {ssh_args}
"""

    with tempfile.NamedTemporaryFile(mode="w", delete=False, suffix=".ini") as inv_file:
        inv_file.write(inventory_content)
        inventory_path = inv_file.name

    # Extra vars for playbook
    extra_vars = f"target_host={server_ip} server_hostname={server_handle}"

    if ssh_public_key:
        extra_vars += f" ssh_public_key='{ssh_public_key}'"

    # Construct ansible-playbook command
    cmd = [
        "ansible-playbook",
        "-i",
        inventory_path,
        playbook_path,
        "--extra-vars",
        extra_vars,
        "-v",
    ]

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
        process = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)  # noqa: S603

        # Log output (abbreviated)
        stdout_brief = (
            process.stdout[:MAX_LOG_LENGTH] + "..."
            if len(process.stdout) > MAX_LOG_LENGTH
            else process.stdout
        )
        logger.debug("ansible_stdout", output=stdout_brief)

        if process.stderr:
            logger.warning("ansible_stderr", output=process.stderr[:MAX_LOG_LENGTH])

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
            output = process.stdout
        else:
            # On failure, capture stderr and the LAST 1000 chars of stdout
            stdout_tail = (
                process.stdout[-MAX_LOG_LENGTH:]
                if len(process.stdout) > MAX_LOG_LENGTH
                else process.stdout
            )
            output = f"STDERR: {process.stderr}\n\nSTDOUT TAIL:\n{stdout_tail}"

        return success, output

    except subprocess.TimeoutExpired:
        logger.error("ansible_playbook_timeout", playbook=playbook_name, timeout=timeout)
        return False, f"Timeout after {timeout}s"
    except Exception as e:
        logger.error(
            "ansible_playbook_exception",
            playbook=playbook_name,
            error=str(e),
            error_type=type(e).__name__,
            exc_info=True,
        )
        return False, str(e)
    finally:
        # Cleanup
        if os.path.exists(inventory_path):
            os.remove(inventory_path)
