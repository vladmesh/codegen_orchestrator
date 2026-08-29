"""The App key must belong to the identity that reads it.

The private key is bind-mounted read-only into the compose services, which run
as `${HOST_UID}:${HOST_GID}`. On the stand it was written by root into a `0700`
root-owned directory, so the services could not traverse to it, and every
contour cleanup died at `PermissionError: [Errno 13] Permission denied:
'/app/keys/github_app.pem'` — invisible for as long as the sweep's verdict was
discarded.

Production has always done this correctly (`chown deploy:deploy /opt/secrets` in
deploy.yml); the stand simply never copied that line.

The fix is ownership, not permission: the directory stays `0700` and the key
`0400`. What this holds down is that one identity is used for both — that the
key's owner and the user the services run as cannot drift apart again.
"""

from pathlib import Path

import yaml

WORKFLOW = Path(__file__).parents[2] / ".github" / "workflows" / "stand-e2e.yml"
RUNTIME_UID = "steps.identity.outputs.runtime_uid"
RUNTIME_GID = "steps.identity.outputs.runtime_gid"


def _steps() -> dict[str, dict]:
    workflow = yaml.safe_load(WORKFLOW.read_text())
    return {step["name"]: step for step in workflow["jobs"]["e2e"]["steps"] if "name" in step}


def test_the_runtime_identity_is_discovered_rather_than_assumed() -> None:
    identity = _steps()["Discover the runtime identity"]
    assert identity.get("id") == "identity"
    assert "GITHUB_OUTPUT" not in _steps()["Bootstrap dynamic orchestrator"]["run"], (
        "the bootstrap handles secret material and publishes no outputs; the identity "
        "lookup belongs in its own step"
    )
    assert "id -u deploy" in identity["run"], (
        "the runtime identity should come from the account bootstrap created, not "
        "from compose's 1000 default, which a cloud image may have given to somebody else"
    )
    assert "runtime_uid=" in identity["run"]
    assert "runtime_gid=" in identity["run"]


def test_the_key_and_the_services_use_one_identity() -> None:
    bring_up = _steps()["Bring up dynamic orchestrator and wait for API"]
    render = _steps()["Render protected dynamic configuration"]

    # What the services run as.
    assert render["env"]["HOST_UID"] == f"${{{{ {RUNTIME_UID} }}}}"
    assert render["env"]["HOST_GID"] == f"${{{{ {RUNTIME_GID} }}}}"

    # What the key belongs to.
    script = bring_up["run"]
    assert "chown ${RUNTIME_UID}:${RUNTIME_GID} /opt/secrets/github_app.pem" in script
    assert "-o ${RUNTIME_UID} -g ${RUNTIME_GID} /opt/secrets" in script


def test_the_fix_is_ownership_and_not_a_wider_mode() -> None:
    script = _steps()["Bring up dynamic orchestrator and wait for API"]["run"]

    assert "install -d -m 0700" in script, "the secrets directory must stay private"
    assert "chmod 0400 /opt/secrets/github_app.pem" in script, "the key must stay read-only"
    for widened in ("0755", "0711", "0644", "chmod +r"):
        assert widened not in script, (
            f"{widened} would hand the key to more than the process that needs it; "
            "the defect was the owner, not the mode"
        )
