"""`OPENROUTER_MANAGEMENT_KEY` reaches the langgraph balance check and nothing else.

Modelled on `CLAUDE_CODE_OAUTH_TOKEN`: the deploy writes an optional environment
secret of the same name into `.env`, and the base compose file hands it to the
one service that reads it. An empty value renders empty, which the service
treats as unset.
"""

from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).parents[2]
NAME = "OPENROUTER_MANAGEMENT_KEY"


class _ComposeLoader(yaml.SafeLoader):
    """Safe YAML that reads compose merge tags (`!reset`, `!override`) as plain values."""


_ComposeLoader.add_multi_constructor(
    "!",
    lambda loader, _tag, node: (
        loader.construct_mapping(node)
        if isinstance(node, yaml.MappingNode)
        else loader.construct_sequence(node)
        if isinstance(node, yaml.SequenceNode)
        else loader.construct_scalar(node)
    ),
)


def _services(compose_file: str) -> dict:
    return yaml.load((ROOT / compose_file).read_text(), Loader=_ComposeLoader)["services"]  # noqa: S506 - a SafeLoader subclass


def _declaring_services(compose_file: str) -> set[str]:
    services = _services(compose_file)
    return {
        name
        for name, service in services.items()
        if NAME in ((service or {}).get("environment") or {})
    }


def test_only_langgraph_is_handed_the_management_key():
    assert _declaring_services("docker-compose.yml") == {"langgraph"}
    for overlay in ("docker-compose.prod.yml", "docker-compose.stand.yml"):
        assert _declaring_services(overlay) == set(), overlay
    langgraph = _services("docker-compose.yml")["langgraph"]
    assert langgraph["environment"][NAME] == "${OPENROUTER_MANAGEMENT_KEY:-}"


def test_the_deploy_writes_the_optional_secret_of_the_same_name():
    workflow = (ROOT / ".github/workflows/deploy.yml").read_text()

    assert f"{NAME}=${{{{ secrets.{NAME} }}}}" in workflow


def test_the_key_is_documented():
    assert f"# {NAME}=" in (ROOT / ".env.example").read_text()
    assert f"| `{NAME}` |" in (ROOT / "docs/DEPLOY.md").read_text()
