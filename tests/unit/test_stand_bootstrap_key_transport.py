"""The bootstrap public key must reach Ansible as one indivisible value.

An SSH public key is `ssh-ed25519 AAAA... comment` — it contains spaces. Ansible
splits its `-e key=value` extra-vars form on whitespace, so a key passed that way
arrives as `ssh-ed25519` alone and `authorized_key` fails with `Module failed:
list index out of range`, which names neither the key nor the truncation.

The adjacent playbook call relies on exactly that splitting to pass two
variables, which is why the mistake reads as correct in review.

This has now cost two runs on separate days: 33181190496 on 2026-08-28 and
33247710739 on 2026-08-29, the second after the fix had already been made and
then lost in a merge. It was lost because nothing held it. This holds it.
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


def test_public_key_is_not_passed_through_splittable_extra_vars() -> None:
    script = _bootstrap_script()
    assert '-e "ssh_public_key=' not in script, (
        "the public key is passed through ansible's whitespace-split `-e key=value` form, "
        "so only `ssh-ed25519` reaches the playbook and authorized_key fails with "
        "`list index out of range`"
    )


def test_public_key_is_passed_as_a_vars_file() -> None:
    script = _bootstrap_script()
    assert '-e "@' in script, (
        "the bootstrap playbook should receive its variables through a file reference, "
        "which is the form ansible cannot split"
    )
    assert "--rawfile ssh_public_key" in script, (
        "the vars file should carry the key as one raw value rather than an interpolated string"
    )
