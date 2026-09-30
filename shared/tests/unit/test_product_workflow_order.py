"""The generated product's workflows, as the pinned template renders them.

The orchestrator orders a product deploy as: registry secrets written, merge,
push-main CI builds and publishes the merge commit's images, those images are
observed, then deploy.yml is dispatched. That order holds only while the
template keeps its half of it, which these tests read from the vendored render
of the pinned template (``scripts/template_pin.py``).
"""

from __future__ import annotations

from pathlib import Path
import re

import yaml

from scripts.template_pin import TEMPLATE_PIN
from shared.clients.github import REGISTRY_SECRET_ENV
from shared.clients.registry import SHA_TAG_PREFIX

REPO_ROOT = Path(__file__).parents[3]
WORKFLOWS = TEMPLATE_PIN.fixture_path(REPO_ROOT) / ".github" / "workflows"


def _workflow(name: str) -> dict:
    return yaml.safe_load((WORKFLOWS / name).read_text())


def _triggers(workflow: dict) -> dict:
    # YAML 1.1 reads the bare key `on` as the boolean True.
    return workflow.get("on") or workflow[True]


def test_deploy_yml_never_starts_on_push():
    """Its only start is the deployer's dispatch, which comes after the images exist."""
    assert set(_triggers(_workflow("deploy.yml"))) == {"workflow_dispatch"}


def test_images_are_built_only_by_push_main_ci():
    """The merge is what builds the images, so the merge is what the secrets precede."""
    workflow = _workflow("ci.yml")
    assert "main" in _triggers(workflow)["push"]["branches"]
    build = workflow["jobs"]["build-and-push"]
    assert build["if"] == "${{ github.event_name == 'push' && github.ref == 'refs/heads/main' }}"


def test_the_build_logs_in_with_exactly_the_secrets_the_merge_refreshes():
    build = (WORKFLOWS / "ci.yml").read_text().split("  build-and-push:", 1)[1]
    read = set(re.findall(r"secrets\.(REGISTRY_[A-Z_]+)", build))
    assert read == {secret for secret, _variable in REGISTRY_SECRET_ENV}


def test_the_build_tags_each_image_with_its_commit():
    """`type=sha` is `sha-<7>`: the tag the deploy asks the registry for."""
    build = _workflow("ci.yml")["jobs"]["build-and-push"]
    meta = next(step for step in build["steps"] if step.get("id") == "meta")
    assert "type=sha" in meta["with"]["tags"].splitlines()
    assert SHA_TAG_PREFIX == "sha-"
