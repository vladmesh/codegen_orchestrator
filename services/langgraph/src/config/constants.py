"""Centralized constants for langgraph service.

All hardcoded paths, timeouts, and magic numbers should be defined here
with environment variable overrides where applicable.
"""

import os


class Paths:
    """File system paths used across the service."""

    SSH_KEY = os.getenv("SSH_KEY_PATH", "/root/.ssh/id_ed25519")


class Timeouts:
    """Timeout values in seconds."""

    # SSH operations
    SSH_COMMAND = int(os.getenv("SSH_COMMAND_TIMEOUT", "30"))

    # Provisioning
    PROVISIONING = int(os.getenv("PROVISIONING_TIMEOUT", "1200"))  # 20 minutes
    REINSTALL = int(os.getenv("REINSTALL_TIMEOUT", "900"))  # 15 minutes
    PASSWORD_RESET = int(os.getenv("PASSWORD_RESET_TIMEOUT", "300"))  # 5 minutes
    ACCESS_PHASE = int(os.getenv("ACCESS_PHASE_TIMEOUT", "180"))  # 3 minutes

    # Worker spawners
    WORKER_SPAWN = int(os.getenv("WORKER_SPAWN_TIMEOUT", "600"))  # 10 minutes
    PREPARER_SPAWN = int(os.getenv("PREPARER_SPAWN_TIMEOUT", "120"))  # 2 minutes

    # Deployment
    SERVICE_DEPLOY = int(os.getenv("SERVICE_DEPLOY_TIMEOUT", "300"))  # 5 minutes


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
