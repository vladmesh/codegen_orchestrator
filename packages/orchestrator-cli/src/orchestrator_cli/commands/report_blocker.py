"""Report-blocker command for developer agent escalation.

When a developer agent hits an unsolvable blocker, this command:
1. Writes BLOCKER.md for admin inspection
2. Outputs ## BLOCKED marker so worker-wrapper detects it
3. Exits cleanly (code 0) — the task freezes, not fails
"""

import sys

import typer

from orchestrator_cli.permissions import require_permission

BLOCKER_MD_PATH = "/home/worker/BLOCKER.md"


@require_permission("report-blocker")
def report_blocker(
    reason: str = typer.Option(..., "--reason", "-r", help="Description of the blocker"),
):
    """Report a blocker that prevents task completion.

    The task will be frozen (WAITING_HUMAN_REVIEW) and an admin will be notified.
    Do NOT commit or push partial/broken code before calling this.

    Examples:
        orchestrator report-blocker --reason "56/78 image URLs return 404"
        orchestrator report-blocker -r "API key for OpenRouter is not configured"
    """
    # Write BLOCKER.md for admin inspection
    try:
        with open(BLOCKER_MD_PATH, "w") as f:
            f.write(f"# Blocker Report\n\n{reason}\n")
    except OSError:
        # Not critical — the marker output is what matters
        pass

    # Output marker to stdout — worker-wrapper ResultParser detects this
    # Use sys.stdout.write to avoid rich console formatting
    sys.stdout.write(f"## BLOCKED\n{reason}\n")
    sys.stdout.flush()

    # Exit cleanly — wrapper interprets exit code 0 + blocked marker
    raise typer.Exit(code=0)
