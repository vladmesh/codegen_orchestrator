"""detect-changes plans the docker legs of a run from the filters its change matched."""

import json

import pytest

from scripts import ci_plan


def _plan(changes, event="pull_request"):
    outputs = ci_plan.plan(set(changes), event)
    return (
        json.loads(outputs["service-legs"]),
        json.loads(outputs["integration-legs"]),
        outputs["service-image-imports"],
    )


def test_a_one_service_pull_request_runs_only_that_service_s_legs():
    service, integration, imports = _plan(["scheduler", "service-images"])

    assert service == ["scheduler"]
    assert integration == ["infra"]
    assert imports == "true"


def test_a_pull_request_touching_no_service_runs_no_docker_leg():
    """A docs-only change used to hold a runner per leg, and 4-6 minutes of imports."""
    assert _plan([]) == ([], [], "false")


@pytest.mark.parametrize("common", ci_plan.COMMON_TRIGGERS)
def test_a_common_change_reaches_every_service_leg(common):
    service, integration, _imports = _plan([common])

    assert service == list(ci_plan.SERVICE_LEGS)
    assert set(integration) >= {"backend", "frontend", "infra", "po-tools"}


def test_the_template_suite_is_reached_by_the_harness_and_the_scaffolder_only():
    assert _plan(["shared"])[1] == ["backend", "frontend", "infra", "po-tools"]
    assert _plan(["scaffolder"])[1] == ["template"]
    assert _plan(["docker-test"])[1] == list(ci_plan.INTEGRATION_LEGS)


def test_a_worker_broker_change_runs_the_stack_that_starts_the_broker():
    service, integration, _imports = _plan(["worker-broker", "service-images"])

    assert service == ["worker-manager"]
    assert integration == []


@pytest.mark.parametrize("change", ["service-images", "deps", "ci"])
def test_the_import_check_runs_for_its_own_filters(change):
    assert _plan([change])[2] == "true"


@pytest.mark.parametrize("change", ["web", "integration-tests", "docker-test"])
def test_the_import_check_is_skipped_when_no_image_changed(change):
    assert _plan([change])[2] == "false"


def test_a_push_to_main_always_runs_the_import_check():
    """The release chain publishes every main commit's images; each is import-checked."""
    assert _plan([], event="push") == ([], [], "true")


def test_a_manual_dispatch_runs_everything():
    service, integration, imports = _plan([], event="workflow_dispatch")

    assert service == list(ci_plan.SERVICE_LEGS)
    assert integration == list(ci_plan.INTEGRATION_LEGS)
    assert imports == "true"


def test_the_outputs_are_written_for_github(tmp_path, monkeypatch):
    output = tmp_path / "github_output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    monkeypatch.setenv("EVENT_NAME", "pull_request")
    monkeypatch.setenv("CHANGES", '["api"]')

    assert ci_plan.main() == 0

    assert output.read_text().splitlines() == [
        'service-legs=["api"]',
        'integration-legs=["backend", "frontend", "infra", "po-tools"]',
        "service-image-imports=false",
    ]


@pytest.mark.parametrize("raw", ["", "null", '"api"', '{"api": true}', "[1]"])
def test_changes_that_are_not_a_list_of_filter_names_fail(raw, tmp_path, monkeypatch):
    """No filter output must never read as "nothing changed": that would skip the suites."""
    monkeypatch.setenv("GITHUB_OUTPUT", str(tmp_path / "github_output"))
    monkeypatch.setenv("EVENT_NAME", "pull_request")
    monkeypatch.setenv("CHANGES", raw)

    with pytest.raises(SystemExit, match="CHANGES is not a JSON list"):
        ci_plan.main()
