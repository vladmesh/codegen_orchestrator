"""Provisioning must not put a QA agent runtime on a deploy target.

Exploratory QA runs centrally. A new server therefore gets no Claude CLI, no
LLM credentials and no Telegram session — and the role that used to install
them is gone rather than merely unused, so it cannot be re-included by a future
playbook edit without someone writing it again.
"""

from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
ANSIBLE = REPO_ROOT / "services/infra-service/ansible"


def test_qa_runner_role_is_gone():
    assert not (ANSIBLE / "roles/qa_runner").exists()


def test_no_playbook_references_the_role():
    for playbook in (ANSIBLE / "playbooks").rglob("*.yml"):
        assert "qa_runner" not in playbook.read_text(), playbook


def test_site_playbook_still_provisions_the_rest():
    [play] = yaml.safe_load((ANSIBLE / "playbooks/site.yml").read_text())
    roles = [role["role"] for role in play["roles"]]

    assert "qa_runner" not in roles
    assert {"common", "security", "docker", "services"} <= set(roles)
