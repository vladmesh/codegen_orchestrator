"""Native configuration render checks stay in CI; contour policy is also offline."""

import ast
import json
import os
from pathlib import Path
import subprocess
from unittest.mock import patch

import pytest
import yaml

from scripts.service_release import compose_override, load_release
from tests.unit.test_production_compose_mounts import BASE, PROD, STAND, _render
from tests.unit.test_stand_e2e_workflow import _render_script

ROOT = Path(__file__).parents[2]


def test_dynamic_stand_environment_carries_fake_platform_configuration(tmp_path, monkeypatch):
    workflow = yaml.safe_load((ROOT / ".github/workflows/stand-e2e.yml").read_text())
    step = next(
        s
        for s in workflow["jobs"]["e2e"]["steps"]
        if s.get("name") == "Render protected dynamic configuration"
    )
    environment = {name: f"value-of-{name}" for name in step["env"]}
    environment.update(
        INTERNAL_API_KEY="stand-internal-token",
        ORCHESTRATOR_HOSTNAME="stand.example.invalid",
        MODEL_SESSIONS="false",
        QA_TELETHON="false",
    )
    monkeypatch.chdir(tmp_path)
    with patch.dict(os.environ, environment, clear=True):
        exec(compile(_render_script(step), "<stand-env-renderer>", "exec"), {})  # noqa: S102 - actual workflow renderer
    values = dict(line.split("=", 1) for line in (tmp_path / ".stand.env").read_text().splitlines())
    assert values["PLATFORM_AUTH_ADMIN_URL"] == "http://stand-fake-platform:8000"
    assert values["PLATFORM_AUTH_ADMIN_TOKEN"] == values["INTERNAL_API_KEY"]
    assert (
        values["PLATFORM_BASE_URL_OVERRIDE"]
        == "https://stand.example.invalid/platform-fake/{service}"
    )


def _expression(text, context):
    """Render the workflow's literal, context, comparison and boolean expressions."""

    def evaluate(node):
        match node:
            case ast.Constant(value=value):
                return value
            case ast.Attribute(value=ast.Name(id=namespace), attr=name):
                return context[namespace][name]
            case ast.Compare(left=left, ops=[ast.Eq()], comparators=[right]):
                return evaluate(left) == evaluate(right)
            case ast.BoolOp(op=ast.And(), values=values):
                result = True
                for value in values:
                    result = evaluate(value)
                    if not result:
                        break
                return result
            case ast.BoolOp(op=ast.Or(), values=values):
                result = False
                for value in values:
                    result = evaluate(value)
                    if result:
                        break
                return result
            case ast.Call(func=ast.Name(id="format"), args=[template, *args]):
                return evaluate(template).format(*(evaluate(arg) for arg in args))
            case _:
                raise AssertionError(ast.dump(node))

    return evaluate(ast.parse(text.replace("&&", " and ").replace("||", " or "), mode="eval").body)


@pytest.mark.parametrize("contour", ["production", "stand", "dev"])
def test_rendered_platform_environment_selects_only_its_contour(contour):
    workflow = yaml.safe_load((ROOT / ".github/workflows/deploy.yml").read_text())
    step = next(
        s for s in workflow["jobs"]["deploy"]["steps"] if s.get("name") == "Write .env to server"
    )
    context = {
        "inputs": {"environment": contour},
        "secrets": {
            "PLATFORM_AUTH_ADMIN_URL": "http://production-admin:8000",
            "PLATFORM_AUTH_ADMIN_TOKEN": "production-token",
            "INTERNAL_API_KEY": "contour-private-token",
            "ORCHESTRATOR_HOSTNAME": "stand.example.invalid",
        },
        "vars": {"STAND_PLATFORM_FIXTURE_FILE": "story.json"},
    }
    names = {
        "PLATFORM_AUTH_ADMIN_URL",
        "PLATFORM_AUTH_ADMIN_TOKEN",
        "PLATFORM_BASE_URL_OVERRIDE",
        "STAND_PLATFORM_FIXTURE_FILE",
    }
    rendered = {}
    for line in step["run"].splitlines():
        name, _, value = line.partition("=")
        if name in names:
            rendered[name] = _expression(
                value.removeprefix("${{").removesuffix("}}").strip(), context
            )
    expected = dict.fromkeys(names, "")
    if contour == "production":
        expected.update(
            PLATFORM_AUTH_ADMIN_URL="http://production-admin:8000",
            PLATFORM_AUTH_ADMIN_TOKEN="production-token",  # noqa: S106 - fixture token
        )
    if contour == "stand":
        expected.update(
            PLATFORM_AUTH_ADMIN_URL="http://stand-fake-platform:8000",
            PLATFORM_AUTH_ADMIN_TOKEN="contour-private-token",  # noqa: S106 - fixture token
            PLATFORM_BASE_URL_OVERRIDE="https://stand.example.invalid/platform-fake/{service}",
            STAND_PLATFORM_FIXTURE_FILE="story.json",
        )
    assert rendered == expected


@pytest.mark.docker
@pytest.mark.parametrize("files", [BASE, PROD, STAND])
def test_native_compose_and_caddy_render(files, tmp_path):
    config = _render(files, tmp_path)
    stand = files == STAND
    assert ("stand-fake-platform" in config["services"]) is stand
    caddy = config["services"]["caddy"]
    mounts = [
        arg
        for v in caddy["volumes"]
        if v["type"] == "bind"
        for arg in ("-v", f"{ROOT / Path(v['source']).relative_to(tmp_path)}:{v['target']}:ro")
    ]
    # Native Caddy adaptation reads exactly the file selected by Compose.
    rendered = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--network=none",
            "--entrypoint",
            "caddy",
            "-e",
            "ORCHESTRATOR_HOSTNAME=stand.example.invalid",
            "-e",
            "REGISTRY_USER=fixture",
            "-e",
            "REGISTRY_PASSWORD_HASH=fixture",
            "-e",
            "LOKI_PUSH_USER=fixture",
            "-e",
            "LOKI_PUSH_PASSWORD_HASH=fixture",
            *mounts,
            caddy["image"],
            "adapt",
            "--config",
            "/etc/caddy/Caddyfile",
            "--adapter",
            "caddyfile",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    native = json.loads(rendered.stdout)
    assert ("platform-fake" in json.dumps(native)) is stand
    if stand:
        fake = config["services"]["stand-fake-platform"]
        assert set(fake["networks"]) == {"internal"}
        assert "ports" not in fake
        assert fake["environment"]["STAND_PLATFORM_FIXTURE_PATH"] == "/fixtures/empty.json"
        release = load_release(ROOT / "tests/unit/fixtures/service-release-7f93d8b7.json")
        override = yaml.safe_load(compose_override(config, release))
        assert (
            override["services"]["stand-fake-platform"]["image"]
            == override["services"]["api"]["image"]
        )
        assert native["apps"]["http"]["servers"]
        assert '"/admin/*"' in json.dumps(native)

        def handlers(value):
            if isinstance(value, dict):
                yield value
                for item in value.values():
                    yield from handlers(item)
            elif isinstance(value, list):
                for item in value:
                    yield from handlers(item)

        assert any(
            handler.get("handler") == "static_response" and str(handler.get("status_code")) == "404"
            for handler in handlers(native)
        )


def test_deploy_writes_fake_credentials_and_override_only_on_stand():
    workflow = yaml.safe_load((ROOT / ".github/workflows/deploy.yml").read_text())
    steps = {s["name"]: s for s in workflow["jobs"]["deploy"]["steps"]}
    script = steps["Write .env to server"]["run"]
    assert "inputs.environment == 'stand' && 'http://stand-fake-platform:8000'" in script
    assert "inputs.environment == 'stand' && secrets.INTERNAL_API_KEY" in script
    assert "PLATFORM_BASE_URL_OVERRIDE=${{ inputs.environment == 'stand' && format(" in script
    assert "https://{0}/platform-fake/{{service}}" in script
    guard = steps["Validate production platform override"]
    assert guard["if"] == "${{ inputs.environment == 'production' }}"
    assert guard["env"]["PLATFORM_BASE_URL_OVERRIDE"] == "${{ vars.PLATFORM_BASE_URL_OVERRIDE }}"


@pytest.mark.subprocess
@pytest.mark.parametrize("override,status", [("", 0), ("https://stand.invalid/{service}", 1)])
def test_workflow_production_guard_executes(override, status):
    workflow = yaml.safe_load((ROOT / ".github/workflows/deploy.yml").read_text())
    step = next(
        s
        for s in workflow["jobs"]["deploy"]["steps"]
        if s.get("name") == "Validate production platform override"
    )
    result = subprocess.run(
        ["bash", "-c", step["run"]],
        env={"PLATFORM_BASE_URL_OVERRIDE": override},
        capture_output=True,
    )
    assert result.returncode == status
