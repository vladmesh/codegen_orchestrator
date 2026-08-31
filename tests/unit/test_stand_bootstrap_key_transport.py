"""The minimal control-plane bootstrap uses root SSH only.

The previous two-play sequence created a deploy SSH account and therefore had
to transport a public key without Ansible splitting it at whitespace. The stand
workflow never connected as that account: root SSH owns bootstrap, checkout,
and the remote runner. The deploy account now exists only as the runtime UID/GID
whose ownership narrows `/opt/secrets`, so carrying an unused public key would
add work and an unnecessary access path to every disposable host.
"""

from pathlib import Path

import yaml

WORKFLOW = Path(__file__).parents[2] / ".github" / "workflows" / "stand-e2e.yml"
BOOTSTRAP_STEP = "Bootstrap dynamic orchestrator"


def _bootstrap_script() -> str:
    workflow = yaml.safe_load(WORKFLOW.read_text())
    steps = workflow["jobs"]["e2e"]["steps"]
    for step in steps:
        if step.get("name") == BOOTSTRAP_STEP:
            return step["run"]
    raise AssertionError(f"the workflow no longer has a {BOOTSTRAP_STEP!r} step")


def test_control_plane_does_not_pass_an_unused_public_key_to_ansible() -> None:
    script = _bootstrap_script()
    assert "ssh_public_key" not in script
    assert "stand-bootstrap.pub" not in script
    assert '-e "@' not in script
