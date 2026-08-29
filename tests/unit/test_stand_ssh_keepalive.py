"""Every remote call in the stand workflow must survive an idle connection.

Several steps hold an SSH session open for minutes while the far side says
nothing: provisioning runs ansible, the suite runs a mega. An idle TCP
connection is exactly what a NAT or a load balancer reclaims, and without
keepalive the client never learns — it either hangs until the job timeout or
dies at `client_loop: send disconnect: Broken pipe`.

Both have happened. Run 33187600987 held a step for 24 minutes after the remote
work had already finished, and run 33249511508 was cut at five silent minutes
during target provisioning.

The options are defined once for the job because repeating them per call is how
one call ends up without them — which is what happened to the earlier fix.
"""

from pathlib import Path
import re

import yaml

WORKFLOW = Path(__file__).parents[2] / ".github" / "workflows" / "stand-e2e.yml"
REQUIRED_OPTIONS = ("ServerAliveInterval", "ServerAliveCountMax")
REMOTE_CALL = re.compile(r"^\s*(?:printf[^|]*\|\s*)?(ssh|scp)\s", re.MULTILINE)


def _e2e_job() -> dict:
    workflow = yaml.safe_load(WORKFLOW.read_text())
    return workflow["jobs"]["e2e"]


def test_the_job_defines_keepalive_once() -> None:
    options = _e2e_job()["env"]["SSH_OPTS"]
    for required in REQUIRED_OPTIONS:
        assert required in options, f"{required} is missing from the shared SSH options"


def test_every_remote_call_uses_the_shared_options() -> None:
    offenders = []
    for step in _e2e_job()["steps"]:
        script = step.get("run")
        if not script or not REMOTE_CALL.search(script):
            continue
        for line in script.splitlines():
            if not REMOTE_CALL.match(line):
                continue
            if "${SSH_OPTS}" not in line:
                offenders.append((step.get("name"), line.strip()))
    assert not offenders, (
        "these remote calls do not carry the shared SSH options, so an idle "
        f"connection can strand them: {offenders}"
    )
