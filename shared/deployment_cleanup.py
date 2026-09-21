"""Runtime-neutral deployment cleanup contract shared by production and recovery."""

from pathlib import Path
import shlex

REMOTE_CLEANUP_SCRIPT = Path(__file__).with_name("deployment_cleanup.sh")


def build_remote_cleanup_command(
    project_name: str,
    service_base: str = "/opt/services",
) -> str:
    """Build the argv-safe command that streams the shared cleanup policy."""
    return shlex.join(["sh", "-s", "--", project_name, service_base.rstrip("/")])
