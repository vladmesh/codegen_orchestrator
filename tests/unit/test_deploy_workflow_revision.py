"""The production deploy must run the commit the workflow was dispatched on.

`git pull origin main` on the host deploys whatever the branch tip happens to be
when the step runs, which is not necessarily the revision that was validated.
"""

from pathlib import Path

import yaml

DEPLOY_WORKFLOW = Path(__file__).parents[2] / ".github" / "workflows" / "deploy.yml"
DEPLOY_SHA = "${{ github.sha }}"


def _step_scripts() -> dict[str, str]:
    workflow = yaml.safe_load(DEPLOY_WORKFLOW.read_text())
    scripts = {}
    for job_name, job in workflow["jobs"].items():
        for step in job["steps"]:
            script = step.get("run") or step.get("with", {}).get("script")
            if script:
                scripts[f"{job_name}/{step['name']}"] = script
    return scripts


def test_deploy_does_not_pull_a_branch_tip():
    offenders = [name for name, script in _step_scripts().items() if "git pull" in script]

    assert not offenders, f"deploy steps must not use git pull: {offenders}"


def test_deploy_does_not_take_its_revision_from_origin_main():
    offenders = [
        name
        for name, script in _step_scripts().items()
        if "origin/main" in script or "origin main" in script
    ]

    assert not offenders, f"deploy steps must not deploy origin/main: {offenders}"


def test_deploy_checks_out_the_dispatched_commit():
    scripts = _step_scripts()
    revision_steps = [name for name, script in scripts.items() if "git fetch" in script]

    assert len(revision_steps) == 1, f"expected one revision step, got {revision_steps}"
    script = scripts[revision_steps[0]]
    assert f"git fetch --no-tags origin {DEPLOY_SHA}" in script
    assert f"git reset --hard {DEPLOY_SHA}" in script
