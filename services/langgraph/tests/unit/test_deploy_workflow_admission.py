"""The bounded recognizer accepts released executable source, not markers."""

import pytest

from scripts.template_pin import TEMPLATE_PIN
from src.subgraphs.devops.deploy_workflow import corrected_ipv6_workflow


def test_actual_pin_render_and_unrelated_names_are_accepted():
    source = (TEMPLATE_PIN.fixture_path() / ".github/workflows/deploy.yml").read_text()
    assert corrected_ipv6_workflow(source)
    customized = "# Reviewed product customization\n" + source.replace(
        "name: Deploy", "name: Owned deploy", 1
    )
    assert corrected_ipv6_workflow(customized)
    assert corrected_ipv6_workflow(
        source.replace("name: Copy compose files to server", "name: Owned copy title")
    )


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
