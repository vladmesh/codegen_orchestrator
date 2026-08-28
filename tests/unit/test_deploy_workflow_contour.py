"""One deploy workflow, two contours — and production unchanged by the split.

The stand reuses production's deploy. Everything that named production directly —
the host path, the SSH user, the compose overlay, the environment itself — is now
read from the selected GitHub Environment. What these tests hold down is that the
defaults are still production's values, and that nothing survived the split as a
hardcoded production name.
"""

from pathlib import Path

import yaml

from shared.provisioning_policy import TIME4VPS_MANAGED_IDS_ENV

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


def test_the_bot_token_never_reaches_another_contour():
    """Two long-polling clients on one token steal each other's updates.

    A repository secret is inherited by every environment that omits it, so
    leaving the token unset for the stand would hand it production's.
    """
    steps = {step["name"]: step for step in _deploy_job()["steps"]}
    env_script = steps["Write .env to server"]["with"]["script"]

    gated = (
        "TELEGRAM_BOT_TOKEN=${{ inputs.environment == 'production' "
        "&& secrets.TELEGRAM_BOT_TOKEN || '' }}"
    )
    assert gated in env_script


def test_a_contour_may_only_use_the_allowlist_it_declared():
    """The same inheritance, where it authorizes reinstalling a server.

    A contour that declares no allowlist receives production's — and with it the
    authority over production's machines. Inheritance cannot be seen at the point
    of use, so the contour states its list as a variable and the deploy refuses
    when the secret does not match it. Checked before anything is written.
    """
    steps = {step["name"]: step for step in _deploy_job()["steps"]}
    guard = steps["Verify this contour's provider allowlist is its own"]

    assert guard["if"] == PRODUCTION_ONLY.replace("==", "!=")
    assert guard["env"]["MANAGED_SERVER_IDS"] == "${{ secrets.TIME4VPS_MANAGED_SERVER_IDS }}"
    assert guard["env"]["DECLARED_SERVER_IDS"] == "${{ vars.MANAGED_SERVER_IDS_DECLARED }}"
    assert "exit 1" in guard["run"]

    names = [step["name"] for step in _deploy_job()["steps"]]
    assert names.index("Verify this contour's provider allowlist is its own") < names.index(
        "Write .env to server"
    )


def test_the_written_env_is_verified_not_the_inputs():
    """The check reads the file the services read, not the value that was sent."""
    steps = {step["name"]: step for step in _deploy_job()["steps"]}
    guard = steps["Verify the deployed contour carries only its own credentials"]

    assert guard["if"] == PRODUCTION_ONLY.replace("==", "!=")
    assert "${{ env.DEPLOY_PATH }}/.env" in guard["with"]["script"]
    assert "MANAGED_SERVER_IDS_DECLARED" in guard["with"]["script"]
    assert "exit 1" in guard["with"]["script"]


def test_deploy_and_provider_policy_use_the_same_runtime_allowlist_key():
    """A deploy cannot demote every managed row by writing an obsolete key."""
    steps = {step["name"]: step for step in _deploy_job()["steps"]}
    env_script = steps["Write .env to server"]["with"]["script"]
    deployed_guard = steps["Verify the deployed contour carries only its own credentials"]["with"][
        "script"
    ]

    expected_env_entry = (
        f"{TIME4VPS_MANAGED_IDS_ENV}=${{{{ secrets.TIME4VPS_MANAGED_SERVER_IDS }}}}"
    )
    assert expected_env_entry in env_script
    assert f"^{TIME4VPS_MANAGED_IDS_ENV}=" in deployed_guard


def test_the_contour_reaches_the_server_env():
    """Services and the sweep read their contour from the deployed .env."""
    steps = {step["name"]: step for step in _deploy_job()["steps"]}
    env_script = steps["Write .env to server"]["with"]["script"]

    assert "LIVE_CONTOUR=${{ vars.LIVE_CONTOUR }}" in env_script


def test_deploys_of_one_contour_do_not_race():
    assert _workflow()["concurrency"]["group"] == "deploy-${{ inputs.environment }}"


def test_a_contour_needs_no_registry_credential_of_its_own():
    """The worker image release is readable without a per-contour secret.

    The base images are public packages of this repository's owner, and where an
    environment defines no GHCR_TOKEN the workflow's automatic token stands in —
    which also still works if those packages are ever made private, since it is
    scoped to this repository. Production keeps using its own secret.
    """
    steps = {step["name"]: step for step in _deploy_job()["steps"]}
    pull = steps["Pull and verify worker base images for this revision"]

    assert "GHCR_TOKEN='${{ secrets.GHCR_TOKEN || github.token }}'" in pull["with"]["script"]
    assert _deploy_job()["permissions"]["packages"] == "read"


def test_a_run_says_which_contour_it_deployed():
    """One workflow serves both contours, so the run list must not be ambiguous.

    Without this the history is a column of identical "Deploy" entries and the
    only way to tell production from the stand is to open the run.
    """
    assert _workflow()["run-name"] == "Deploy ${{ inputs.environment }}"
