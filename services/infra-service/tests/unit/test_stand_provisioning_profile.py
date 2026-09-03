"""The Stand-only target profile keeps production provisioning intact."""

from pathlib import Path

import yaml

ANSIBLE_DIR = Path(__file__).parents[2] / "ansible"
SOFTWARE_PLAYBOOK = ANSIBLE_DIR / "playbooks" / "provision_software.yml"
SITE_PLAYBOOK = ANSIBLE_DIR / "playbooks" / "site.yml"
QA_IDENTITY_TASKS = ANSIBLE_DIR / "roles" / "qa_identity" / "tasks" / "main.yml"
PROVISION_VARS = ANSIBLE_DIR / "group_vars" / "provision_vars.yml"


def _task(tasks: list[dict], name: str) -> dict:
    return next(task for task in tasks if task["name"] == name)


def test_production_keeps_the_full_dist_upgrade_but_stand_explicitly_skips_it():
    playbook = yaml.safe_load(SOFTWARE_PLAYBOOK.read_text())[0]
    upgrade = _task(playbook["tasks"], "Upgrade all packages")

    assert playbook["vars"]["provisioning_profile"] == "production"
    assert upgrade["apt"]["upgrade"] == "dist"
    assert upgrade["when"] == "provisioning_profile != 'stand_e2e'"


def test_stand_profile_consolidates_required_packages_and_keeps_all_target_roles():
    tasks = yaml.safe_load(SOFTWARE_PLAYBOOK.read_text())[0]["tasks"]
    essentials = yaml.safe_load(PROVISION_VARS.read_text())["essential_packages"]
    roles = {
        task["ansible.builtin.include_role"]["name"]
        for task in tasks
        if "ansible.builtin.include_role" in task
    }
    qa_tasks = yaml.safe_load(QA_IDENTITY_TASKS.read_text())

    assert "acl" in essentials
    assert {"deploy_target", "qa_identity", "monitoring"}.issubset(roles)
    assert "Install acl, so reads can be granted without a group that can write" in {
        task["name"] for task in qa_tasks
    }


def test_provisioning_emits_bounded_phase_timings_for_live_comparison():
    tasks = yaml.safe_load(SOFTWARE_PLAYBOOK.read_text())[0]["tasks"]
    names = {task["name"] for task in tasks}

    assert {
        "Record apt lock wait started",
        "Record apt lock wait finished",
        "Record package and system installation finished",
        "Record Docker and tooling installation finished",
        "Record roles and monitoring finished",
        "Report provisioning phase timings",
    }.issubset(names)


def test_site_playbook_provisions_the_base_roles():
    [play] = yaml.safe_load(SITE_PLAYBOOK.read_text())
    roles = {role["role"] for role in play["roles"]}

    assert {"common", "security", "docker", "services"} <= roles


def test_the_profile_can_skip_the_dist_upgrade_and_nothing_else():
    """What `stand_e2e` is allowed to be: a smaller upgrade, not a smaller host.

    Run 33718999040 reached a `stand_e2e` target recorded complete whose QA
    account had no `authorized_keys`, and the first suspect was this profile.
    It is not: the only task in the whole software phase whose `when:` mentions
    the profile is the dist-upgrade. This holds that answer, so a later branch
    on the profile has to be looked at rather than discovered on a paid run.
    """
    tasks = yaml.safe_load(SOFTWARE_PLAYBOOK.read_text())[0]["tasks"]
    gated = [task["name"] for task in tasks if "provisioning_profile" in str(task.get("when", ""))]

    assert gated == ["Upgrade all packages"]


def test_the_qa_identity_role_runs_under_every_profile():
    """Unconditional, and not merely unconditional today.

    The include carries no condition of its own, and the play ends by reporting
    what the target said about the account — a variable the role's own proof
    registers. So a run that skipped the role, under any profile or any task
    selection somebody adds later, fails on that report instead of reaching
    `provisioning_phase=complete` with no QA seat on the host.
    """
    play = yaml.safe_load(SOFTWARE_PLAYBOOK.read_text())[0]
    include = _task(play["tasks"], "Create the QA run identity")
    report = _task(play["tasks"], "Report the QA identity this host lends")
    proof = [
        task
        for task in yaml.safe_load(QA_IDENTITY_TASKS.read_text())
        if task.get("register") == "qa_identity_proof"
    ]

    assert "when" not in include
    assert include["ansible.builtin.include_role"]["name"] == "qa_identity"
    assert "qa_identity_proof.stdout" in str(report["ansible.builtin.debug"]["msg"])
    assert "when" not in report
    # The variable the report reads is registered by the role, and by the task
    # that is the role's last word — so the report is undefined unless the proof
    # itself ran and succeeded.
    assert len(proof) == 1
    assert "qa-identity-proof" in proof[0]["ansible.builtin.script"]["cmd"]
