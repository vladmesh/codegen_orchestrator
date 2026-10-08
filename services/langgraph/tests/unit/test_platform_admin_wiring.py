"""Deploy configuration isolates platform administration to the production caller."""

from pathlib import Path

import yaml

ROOT = Path(__file__).parents[4]


class ComposeLoader(yaml.SafeLoader):
    """Read Compose merge tags for offline configuration checks."""


def _tag(loader, node):
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node, deep=True)
    if isinstance(node, yaml.MappingNode):
        return loader.construct_mapping(node, deep=True)
    return loader.construct_scalar(node)


for tag in ("!override", "!reset"):
    ComposeLoader.add_constructor(tag, _tag)


def _compose(name):
    return yaml.load((ROOT / name).read_text(), Loader=ComposeLoader)  # noqa: S506


def test_production_attaches_the_auth_caller_to_the_external_link():
    prod = _compose("docker-compose.prod.yml")
    assert prod["networks"]["platform_auth"] == {
        "external": True,
        "name": "codegen-orch-link",
    }
    callers = {
        name
        for name, service in prod["services"].items()
        if "platform_auth" in service.get("networks", [])
    }
    assert callers == {"deploy-worker"}
    base = _compose("docker-compose.yml")
    assert base["services"]["deploy-worker"]["env_file"] == [".env"]


def test_stand_retains_all_base_networks_and_the_deploy_worker_internal_network():
    base = _compose("docker-compose.yml")
    stand = _compose("docker-compose.stand.yml")
    assert stand["networks"] == base["networks"]
    assert stand["services"]["deploy-worker"]["networks"] == ["internal"]


def test_platform_docker_logs_are_discovered_and_labelled_by_compose_project():
    config = yaml.safe_load((ROOT / "infra/promtail.yml").read_text())
    [docker] = config["scrape_configs"]
    selectors = [
        value
        for source in docker["docker_sd_configs"]
        for filter_ in source["filters"]
        if filter_["name"] == "label"
        for value in filter_["values"]
    ]
    assert "com.docker.compose.project=codegen_platform" in selectors
    assert "com.docker.compose.project=codegen_orchestrator" in selectors
    assert "com.codegen.type=worker" in selectors
    assert {
        "source_labels": ["__meta_docker_container_label_com_docker_compose_project"],
        "target_label": "compose_project",
    } in docker["relabel_configs"]


def test_admin_credentials_are_validated_and_written_only_for_production():
    workflow = yaml.safe_load((ROOT / ".github/workflows/deploy.yml").read_text())
    steps = {step["name"]: step for step in workflow["jobs"]["deploy"]["steps"]}
    validation = steps["Validate production platform auth secrets"]
    assert validation["if"] == "${{ inputs.environment == 'production' }}"
    script = steps["Write .env to server"]["run"]
    for name in ("PLATFORM_AUTH_ADMIN_URL", "PLATFORM_AUTH_ADMIN_TOKEN"):
        assert validation["env"][f"REQUIRED_{name}"] == f"${{{{ secrets.{name} }}}}"
        assert f"REQUIRED_{name}" in validation["run"]
        assert (
            f"{name}=${{{{ inputs.environment == 'production' && secrets.{name} || '' }}}}"
        ) in script
