"""The bounded recognizer accepts released executable source, not markers."""

from hashlib import sha256
import json
from pathlib import Path

import pytest

from scripts.template_pin import TEMPLATE_PIN
from src.subgraphs.devops.deploy_workflow import corrected_ipv6_workflow

RETAINED_RELEASE = Path(__file__).parent / "fixtures/released-deploy-workflow"


def retained_release_source():
    """The SSH-action workflow an earlier pin rendered, checked against its producer hash."""
    provenance = json.loads((RETAINED_RELEASE / "provenance.json").read_text())
    source = (RETAINED_RELEASE / "ssh-action-deploy.yml").read_bytes()
    assert sha256(source).hexdigest() == provenance["sha256"]
    return source.decode()


def pin_release_source():
    return (TEMPLATE_PIN.fixture_path() / ".github/workflows/deploy.yml").read_text()


def test_actual_pin_render_and_unrelated_names_are_accepted():
    source = pin_release_source()
    assert corrected_ipv6_workflow(source)
    customized = "# Reviewed product customization\n" + source.replace(
        "name: Deploy", "name: Owned deploy", 1
    )
    assert corrected_ipv6_workflow(customized)
    assert corrected_ipv6_workflow(
        source.replace("name: Copy compose files to server", "name: Owned copy title")
    )


@pytest.mark.parametrize("release", [retained_release_source, pin_release_source])
def test_each_reviewed_release_is_accepted_only_with_its_exact_execution(release):
    source = release()
    assert corrected_ipv6_workflow(source)
    assert not corrected_ipv6_workflow(source.replace("scp $SSH_OPTS", "scp -O $SSH_OPTS"))
    assert not corrected_ipv6_workflow(source.replace("runs-on: ubuntu-24.04", "runs-on: x"))


@pytest.mark.parametrize(
    "source",
    [
        None,
        "",
        "jobs: []",
        "jobs: {deploy: null}",
        "on: [workflow_dispatch]\njobs: {}",
        "&job {jobs: *job}",
        "x" * 65_537,
    ],
)
def test_unreadable_or_ambiguous_source_is_refused(source):
    assert not corrected_ipv6_workflow(source)
