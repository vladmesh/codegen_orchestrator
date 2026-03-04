"""Centralized constants shared across services.

All hardcoded paths, timeouts, and magic numbers should be defined here
with environment variable overrides where applicable.
"""

import os


class Paths:
    """File system paths used across services."""

    SSH_KEY = os.getenv("SSH_KEY_PATH", "/root/.ssh/id_ed25519")
    ANSIBLE_PLAYBOOKS = os.getenv(
        "ANSIBLE_PLAYBOOKS_PATH",
        "/app/ansible/playbooks",
    )

    @classmethod
    def playbook(cls, name: str) -> str:
        """Get full path to an Ansible playbook."""
        return f"{cls.ANSIBLE_PLAYBOOKS}/{name}"


class Timeouts:
    """Timeout values in seconds."""

    # SSH operations
    SSH_COMMAND = int(os.getenv("SSH_COMMAND_TIMEOUT", "30"))

    # Provisioning
    PROVISIONING = int(os.getenv("PROVISIONING_TIMEOUT", "1200"))  # 20 minutes
    REINSTALL = int(os.getenv("REINSTALL_TIMEOUT", "900"))  # 15 minutes
    PASSWORD_RESET = int(os.getenv("PASSWORD_RESET_TIMEOUT", "300"))  # 5 minutes
    ACCESS_PHASE = int(os.getenv("ACCESS_PHASE_TIMEOUT", "180"))  # 3 minutes

    # Worker spawners (langgraph-specific but shared for visibility)
    WORKER_SPAWN = int(os.getenv("WORKER_SPAWN_TIMEOUT", "1800"))  # 30 minutes
    PREPARER_SPAWN = int(os.getenv("PREPARER_SPAWN_TIMEOUT", "120"))  # 2 minutes

    # Deployment
    SERVICE_DEPLOY = int(os.getenv("SERVICE_DEPLOY_TIMEOUT", "300"))  # 5 minutes


class CI:
    """CI monitoring constants."""

    # Maximum times to re-spawn developer after CI failure
    MAX_FIX_RETRIES = int(os.getenv("CI_MAX_FIX_RETRIES", "2"))

    # Timeout waiting for ci.yml to complete (seconds)
    WORKFLOW_TIMEOUT = int(os.getenv("CI_WORKFLOW_TIMEOUT", "600"))  # 10 minutes

    # Poll interval for CI status (seconds)
    POLL_INTERVAL = int(os.getenv("CI_POLL_INTERVAL", "15"))

    # Total gate timeout for the entire CI fix loop (seconds)
    TOTAL_GATE_TIMEOUT = int(os.getenv("CI_TOTAL_GATE_TIMEOUT", "3600"))  # 60 minutes

    # CI workflow filename
    CI_WORKFLOW_FILE = os.getenv("CI_WORKFLOW_FILE", "ci.yml")


class Provisioning:
    """Provisioning-related constants."""

    MAX_RETRIES = int(os.getenv("PROVISIONING_MAX_RETRIES", "3"))
    PASSWORD_RESET_POLL_INTERVAL = int(os.getenv("PASSWORD_RESET_POLL_INTERVAL", "5"))
    REINSTALL_POLL_INTERVAL = int(os.getenv("REINSTALL_POLL_INTERVAL", "15"))
    POST_REINSTALL_BOOT_WAIT = int(os.getenv("POST_REINSTALL_BOOT_WAIT", "60"))

    # Default OS template for reinstall
    DEFAULT_OS_TEMPLATE = os.getenv(
        "DEFAULT_OS_TEMPLATE",
        "kvm-ubuntu-24.04-gpt-x86_64",
    )
