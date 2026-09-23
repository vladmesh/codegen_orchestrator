"""The production deploy must run the one revision the workflow run deploys.

`git pull origin main` on the host deploys whatever the branch tip happens to be
when the step runs, which is not necessarily the revision that was validated. The
revision is the dispatched commit or, for a rollback, the `revision` input
(`DEPLOY_REVISION`); tests/unit/test_deploy_service_release.py pins that every step
uses that one value.
"""

from pathlib import Path
import re

import yaml

DEPLOY_WORKFLOW = Path(__file__).parents[2] / ".github" / "workflows" / "deploy.yml"
DEPLOY_SHA = "${{ env.DEPLOY_REVISION }}"
# `git fetch`, or `git -C <path> fetch` for a fetch into another tree.
GIT_FETCH = re.compile(r"\bgit\s+(?:-C\s+\S+\s+)?fetch\b")


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


def test_deploy_checks_out_the_deployed_revision():
    """Fetched and staged first, the live tree reset to it only at the switch.

    The fetch writes only into .git, which no container mounts; the reset moves the
    bind-mounted sources, so it is a later step (tests/unit/test_deploy_service_release.py
    pins that it follows every pull and check).
    """
    scripts = _step_scripts()
    names = list(scripts)
    fetch_steps = [name for name, script in scripts.items() if GIT_FETCH.search(script)]
    reset_steps = [name for name, script in scripts.items() if "git reset" in script]

    assert len(fetch_steps) == 1, f"expected one fetch step, got {fetch_steps}"
    assert len(reset_steps) == 1, f"expected one reset step, got {reset_steps}"
    assert f"fetch --no-tags origin {DEPLOY_SHA}" in scripts[fetch_steps[0]]
    assert f"git reset --hard {DEPLOY_SHA}" in scripts[reset_steps[0]]
    assert names.index(fetch_steps[0]) < names.index(reset_steps[0])
