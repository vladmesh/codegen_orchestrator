"""The buildx layer cache is scoped per Dockerfile, so every build of one image shares it."""

import json
from pathlib import Path
import subprocess

import pytest

from scripts import ci_build_cache as cache

ROOT = Path(__file__).resolve().parents[2]
TEST_COMPOSE_FILES = sorted((ROOT / "tests" / "compose").glob("*/*.yml"))


def _config(**builds):
    """A resolved compose config, the shape `docker compose config --format json` prints."""
    services = {"redis": {"image": "redis:7.4.10-alpine"}}
    for name, dockerfile in builds.items():
        services[name] = {"build": {"context": str(ROOT), "dockerfile": dockerfile}}
    return {"services": services}


def test_one_dockerfile_is_one_scope_whichever_service_or_file_builds_it():
    service_leg = cache.compose_override(_config(api="services/api/Dockerfile"))
    integration_leg = cache.compose_override(
        _config(api="services/api/Dockerfile", runner="tests/compose/integration/Dockerfile")
    )

    assert service_leg["services"]["api"] == integration_leg["services"]["api"]
    assert service_leg["services"]["api"]["build"] == {
        "cache_from": ["type=gha,scope=buildx-services_api_Dockerfile"],
        "cache_to": ["type=gha,scope=buildx-services_api_Dockerfile,mode=max,ignore-error=true"],
    }
    assert integration_leg["services"]["runner"]["build"]["cache_from"] == [
        "type=gha,scope=buildx-tests_compose_integration_Dockerfile"
    ]


def test_two_dockerfiles_never_share_a_scope():
    scopes = {
        cache.scope(dockerfile)
        for dockerfile in (
            "services/api/Dockerfile",
            "services/api/Dockerfile.test",
            "services/langgraph/Dockerfile",
            "tests/compose/integration/Dockerfile",
            "tests/compose/integration/Dockerfile.template",
        )
    }

    assert len(scopes) == 5


def test_the_scope_is_the_repository_path_whatever_the_build_context():
    config = {
        "services": {
            "api": {"build": {"context": str(ROOT / "services"), "dockerfile": "api/Dockerfile"}}
        }
    }

    override = cache.compose_override(config)

    assert override["services"]["api"]["build"]["cache_from"] == [
        "type=gha,scope=buildx-services_api_Dockerfile"
    ]


def test_services_that_only_pull_are_left_out():
    override = cache.compose_override(_config(api="services/api/Dockerfile"))

    assert set(override["services"]) == {"api"}


def test_a_compose_file_that_builds_nothing_is_refused():
    with pytest.raises(ValueError, match="builds nothing"):
        cache.compose_override(_config())


@pytest.mark.parametrize("dockerfile", ["/etc/Dockerfile", "../outside/Dockerfile", ""])
def test_a_dockerfile_outside_the_repository_has_no_scope(dockerfile):
    with pytest.raises(ValueError):
        cache.scope(dockerfile)


def test_writing_the_cache_never_fails_a_build_and_keeps_every_stage():
    """A throttled cache service costs speed, not a red build; builder stages are kept."""
    (entry,) = cache.cache_to("services/api/Dockerfile")

    assert "ignore-error=true" in entry.split(",")
    assert "mode=max" in entry.split(",")


def test_the_override_is_written_as_the_resolved_file_says(tmp_path, monkeypatch):
    monkeypatch.setattr(
        cache,
        "resolved_compose",
        lambda compose_file: _config(api="services/api/Dockerfile"),
    )
    output = tmp_path / "override.yml"

    assert cache.main(["compose-override", "tests/compose/service/api.yml", str(output)]) == 0

    assert json.loads(output.read_text())["services"]["api"]["build"]["cache_to"] == (
        cache.cache_to("services/api/Dockerfile")
    )


@pytest.mark.skipif(
    subprocess.run(["docker", "compose", "version"], capture_output=True).returncode != 0,
    reason="docker compose is not installed",
)
@pytest.mark.parametrize("compose_file", TEST_COMPOSE_FILES, ids=lambda path: path.stem)
def test_every_test_compose_file_gets_a_cache_for_every_image_it_builds(compose_file):
    """`docker compose config` only parses: nothing is built or pulled."""
    config = cache.resolved_compose(str(compose_file))

    override = cache.compose_override(config)

    built = {name for name, service in config["services"].items() if "build" in service}
    assert set(override["services"]) == built
