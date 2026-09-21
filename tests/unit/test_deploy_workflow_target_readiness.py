"""The production deploy reconciles every managed target once its services are healthy.

The receipt admission and QA read is only as current as the last reconciliation,
so the deploy that ships a new `qa_identity` role is the one that has to apply
it — against the revision it deployed, after the services that record verdicts
are up, and before it reports success.
"""

from pathlib import Path

import yaml

DEPLOY_WORKFLOW = Path(__file__).parents[2] / ".github" / "workflows" / "deploy.yml"
DEPLOY_SHA = "${{ github.sha }}"
RECONCILE = "python -m src.provisioner.target_readiness"
STEP = "Reconcile managed deploy targets"


def _steps() -> list[dict]:
    return yaml.safe_load(DEPLOY_WORKFLOW.read_text())["jobs"]["deploy"]["steps"]


def _script(step: dict) -> str:
    return step.get("run") or step.get("with", {}).get("script") or ""


def _index(name: str) -> int:
    return next(index for index, step in enumerate(_steps()) if step["name"] == name)


def test_exactly_one_step_reconciles_and_it_is_production_only():
    reconciling = [step for step in _steps() if RECONCILE in _script(step)]

    assert [step["name"] for step in reconciling] == [STEP]
    assert reconciling[0]["if"] == "${{ inputs.environment == 'production' }}"


def test_it_runs_after_the_new_services_are_healthy_and_before_the_deploy_ends():
    reconcile = _index(STEP)

    assert _index("Deploy") < reconcile
    assert _index("Run migrations") < reconcile
    assert _index("Health check") < reconcile
    assert _index("Wait for scheduler services") < reconcile
    assert reconcile < _index("Cleanup")
    assert reconcile == len(_steps()) - 2


def test_it_is_bound_to_the_exact_deployed_revision():
    script = _script(_steps()[_index(STEP)])

    assert "deployed=$(git rev-parse HEAD)" in script
    assert f'[ "${{deployed}}" != "{DEPLOY_SHA}" ]' in script
    assert f"{RECONCILE} --revision {DEPLOY_SHA}" in script
    assert "exec -T infra-service" in script


def test_a_failed_reconciliation_fails_the_deploy():
    step = _steps()[_index(STEP)]
    script = _script(step)

    assert script.lstrip().startswith("set -euo pipefail")
    assert "|| true" not in script
    assert not step.get("continue-on-error")


def test_it_never_reinstalls_rebuilds_or_starts_paid_work():
    script = _script(_steps()[_index(STEP)]).lower()

    for forbidden in (
        "force-rebuild",
        "provision_access",
        "provision_software",
        "ufw",
        "stand-e2e",
        "recheck-qa",
        "qa_identity_retrofit.yml",
    ):
        assert forbidden not in script
