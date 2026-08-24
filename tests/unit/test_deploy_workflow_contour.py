"""One deploy workflow, two contours — and production unchanged by the split.

The stand reuses production's deploy. Everything that named production directly —
the host path, the SSH user, the compose overlay, the environment itself — is now
read from the selected GitHub Environment. What these tests hold down is that the
defaults are still production's values, and that nothing survived the split as a
hardcoded production name.
"""

from pathlib import Path

import yaml

DEPLOY_WORKFLOW = Path(__file__).parents[2] / ".github" / "workflows" / "deploy.yml"
PRODUCTION_ONLY = "${{ inputs.environment == 'production' }}"


def _workflow() -> dict:
    workflow = yaml.safe_load(DEPLOY_WORKFLOW.read_text())
    # YAML reads a bare `on:` key as the boolean True, so the trigger block is
    # not where its name says it is.
    workflow["on"] = workflow.get("on", workflow.get(True))
    return workflow


def _deploy_job() -> dict:
    return _workflow()["jobs"]["deploy"]


def _steps_with_ssh() -> list[dict]:
    return [step for step in _deploy_job()["steps"] if "username" in step.get("with", {})]


def test_the_environment_is_the_dispatched_one():
    assert _deploy_job()["environment"] == "${{ inputs.environment }}"


def test_the_stand_is_dispatchable():
    options = _workflow()["on"]["workflow_dispatch"]["inputs"]["environment"]["options"]

    assert options == ["production", "stand"]


def test_defaults_are_production_so_an_unset_environment_deploys_as_before():
    """A contour that defines no variables gets exactly production's deploy."""
    env = _deploy_job()["env"]

    assert env["DEPLOY_PATH"] == "${{ vars.DEPLOY_PATH || '/opt/codegen_orchestrator' }}"
    assert env["DEPLOY_SSH_USER"] == "${{ vars.DEPLOY_SSH_USER || 'deploy' }}"
    assert env["COMPOSE_ARGS"] == (
        "${{ vars.COMPOSE_ARGS || '-f docker-compose.yml -f docker-compose.prod.yml' }}"
    )


def test_no_step_hardcodes_the_production_ssh_user():
    """A missed `username: deploy` would send a stand deploy at the wrong account."""
    offenders = [
        step["name"]
        for step in _steps_with_ssh()
        if step["with"]["username"] != "${{ env.DEPLOY_SSH_USER }}"
    ]

    assert not offenders, f"steps still naming a fixed SSH user: {offenders}"


def test_no_step_hardcodes_the_production_compose_overlay():
    """The stand adds an overlay of its own; a fixed pair of -f flags ignores it."""
    offenders = []
    for step in _deploy_job()["steps"]:
        script = step.get("run") or step.get("with", {}).get("script") or ""
        # Only a compose invocation counts. The .env heredoc mentions the
        # overlay by name in a comment, and that is not a fixed overlay.
        if "-f docker-compose.prod.yml" in script:
            offenders.append(step["name"])

    assert not offenders, f"steps still naming the production overlay: {offenders}"


def test_provider_secrets_are_required_of_production_only():
    """The stand has no provider credentials, and requiring them would block it."""
    steps = {step["name"]: step for step in _deploy_job()["steps"]}
    provider = steps["Validate production provider secrets"]

    assert provider["if"] == PRODUCTION_ONLY
    assert "TIME4VPS_LOGIN" in yaml.dump(provider["env"])

    shared = steps["Validate required secrets"]
    assert "if" not in shared
    assert "TIME4VPS" not in yaml.dump(shared["env"])


def test_a_non_production_contour_is_refused_a_provider_allowlist():
    """Default-deny is what keeps a stand run away from a production server.

    An allowlist is the only thing that authorizes provisioning or reinstalling a
    Time4VPS server. A stand that carried one could act on a machine it does not
    own, so the deploy refuses to start rather than trusting it to be the right
    list.
    """
    steps = {step["name"]: step for step in _deploy_job()["steps"]}
    guard = steps["Refuse a provider allowlist outside production"]

    assert guard["if"] == "${{ inputs.environment != 'production' }}"
    assert "exit 1" in guard["run"]


def test_the_contour_reaches_the_server_env():
    """Services and the sweep read their contour from the deployed .env."""
    steps = {step["name"]: step for step in _deploy_job()["steps"]}
    env_script = steps["Write .env to server"]["with"]["script"]

    assert "LIVE_CONTOUR=${{ vars.LIVE_CONTOUR }}" in env_script


def test_deploys_of_one_contour_do_not_race():
    assert _workflow()["concurrency"]["group"] == "deploy-${{ inputs.environment }}"
