"""Centralized constants shared across services.

All hardcoded paths, timeouts, and magic numbers should be defined here
with environment variable overrides where applicable.
"""

import os


class Paths:
    """File system paths used across services."""

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

    # How long one coding-agent turn may run inside a worker before the wrapper
    # stops it. This is the deliberate limit on the work itself, enforced by the
    # process that runs it, and it is the only timer that may end a working
    # engineering worker. Real product tasks — a business-logic review that runs
    # unit and integration suites, a Copier migration that generates, resolves
    # conflicts, tests and builds Docker images — take tens of minutes.
    AGENT_TURN = int(os.getenv("AGENT_TURN_TIMEOUT", "3600"))  # 60 minutes

    # What a turn costs on top of the agent process: workspace pull, venv
    # repointing, transcript save, commit, push and result submission. Every
    # timer that waits for a turn is derived from AGENT_TURN plus this, so no
    # observer can expire before the limit it is waiting on.
    WORKER_TURN_OVERHEAD = int(os.getenv("WORKER_TURN_OVERHEAD_TIMEOUT", "900"))  # 15 minutes

    # Worker spawners (langgraph-specific but shared for visibility). This is an
    # observer's wait, not a limit: it must outlast the turn it waits for, or it
    # would take away a worker that is still within its own limit.
    WORKER_SPAWN = int(os.getenv("WORKER_SPAWN_TIMEOUT", str(AGENT_TURN + WORKER_TURN_OVERHEAD)))
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
