"""The Stand-only target profile keeps production provisioning intact."""

from pathlib import Path

import yaml

ANSIBLE_DIR = Path(__file__).parents[2] / "ansible"
SOFTWARE_PLAYBOOK = ANSIBLE_DIR / "playbooks" / "provision_software.yml"
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
