"""Issuance evidence is accepted only for the deployed contract's declared grant."""

import builtins
from contextlib import redirect_stdout
from copy import deepcopy
import hashlib
from io import StringIO
import shlex
import shutil
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

from framework.generators.package_environment import PackageEnvironmentGenerator
from framework.spec.events import EventsSpec
from framework.spec.loader import AllSpecs
from framework.spec.models import ModelsSpec
from framework.spec.package_resolution import ActivePackage
from framework.spec.packages import parse_package_manifest
import pytest
import yaml

from scripts import template_pin
from shared.contracts.env_contract import EnvContractMergeError
from shared.live_harness_platform import issuance_proof


def _product_fixture(tmp_path, monkeypatch, moved_pin):
    if moved_pin:
        pin = template_pin.TemplatePin(source="gh:fixture/candidate-kit", ref="candidate")
        moved_fixture = pin.fixture_path(tmp_path)
        shutil.copytree(template_pin.TEMPLATE_PIN.fixture_path(), moved_fixture)
        contract_path = moved_fixture / "services/backend/env.contract.yaml"
        contract = yaml.safe_load(contract_path.read_text())
        contract["entries"]["APP_NAME"]["description"] = "Candidate fixture declaration"
        contract_path.write_text(yaml.safe_dump(contract))
        monkeypatch.setattr(template_pin, "TEMPLATE_PIN", pin)
        fixture_path = template_pin.TemplatePin.fixture_path
        monkeypatch.setattr(
            template_pin.TemplatePin, "fixture_path", lambda self: fixture_path(self, tmp_path)
        )
    return template_pin.TEMPLATE_PIN.fixture_path()


@pytest.mark.parametrize(("conflict", "moved_pin"), [(False, False), (True, False), (False, True)])
async def test_backend_probe_merges_generated_package_fragment(
    tmp_path, monkeypatch, conflict, moved_pin
):
    from shared import live_harness_mechanical_readback as readback

    fixture = _product_fixture(tmp_path, monkeypatch, moved_pin)
    product = tmp_path / "product"
    shutil.copytree(fixture, product)
    bot = tmp_path / "bot"
    shutil.copytree(product / "services/tg_bot", bot / "services/tg_bot")
    manifest, _, admin = facts()
    manifest.update(
        protocol_version=1,
        name="fictional-module",
        version="1.0.0",
        requires_core=">=2.4,<3",
        http={"prefix": "/fictional"},
    )
    site = tmp_path / "site"
    module = site / "fictional_module"
    module.mkdir(parents=True)
    (module / "__init__.py").write_text("")
    (module / "package.yaml").write_text(yaml.safe_dump(manifest))
    metadata = site / "fictional_dist-1.0.0.dist-info"
    metadata.mkdir()
    (metadata / "METADATA").write_text("Name: fictional-dist\nVersion: 1.0.0\n")
    active = ActivePackage(
        name=manifest["name"],
        manifest=parse_package_manifest(manifest),
        package_root=module,
        manifest_sha256=hashlib.sha256((module / "package.yaml").read_bytes()).hexdigest(),
    )
    specs = AllSpecs(models=ModelsSpec(models={}), events=EventsSpec(), packages=[active])
    PackageEnvironmentGenerator(specs, product).generate()
    if conflict:
        fragment = yaml.safe_load(
            (product / "services/backend/packages/env.contract.yaml").read_text()
        )
        fragment["owner"] = "another-owner"
        fragment["entries"]["KEY"]["scopes"] = ["foreign"]
        (product / "services/backend/another.env.contract.yaml").write_text(
            yaml.safe_dump(fragment)
        )
    identity = {key: manifest[key] for key in ("name", "version")}
    identity["manifest_sha256"] = active.manifest_sha256
    (product / "codegen_kit/_active_packages.py").write_text(f"ACTIVE_PACKAGES = {[identity]!r}\n")
    monkeypatch.syspath_prepend(str(site))
    monkeypatch.syspath_prepend(str(product))
    # Record each prior module so monkeypatch also cleans up the fixture imports.
    for name in (
        "codegen_kit",
        "codegen_kit.packages",
        "codegen_kit._active_packages",
        "fictional_module",
    ):
        monkeypatch.setitem(sys.modules, name, None)
        del sys.modules[name]

    def ssh(_destination, _key, command, _stdin, **kwargs):
        argv = shlex.split(command)
        if argv[1] == "ps":
            service = next(arg.rsplit("=", 1)[1] for arg in argv if "compose.service=" in arg)
            stdout = service
        elif argv[1] == "inspect":
            stdout = '"image-id" "ghcr.io/owner/product:release"'
        elif argv[1:3] == ["image", "inspect"]:
            stdout = '["ghcr.io/owner/product@sha256:abc"]'
        else:
            assert argv[:2] == ["docker", "exec"]
            root = product if argv[2] == "backend" else bot
            script = argv[-1].replace("Path('/app')", f"Path({str(root)!r})")
            output = StringIO()
            with redirect_stdout(output):
                exec(script, {})  # noqa: S102 - run the actual container probe on fixture files
            stdout = output.getvalue()
        return SimpleNamespace(returncode=0, stdout=stdout)

    monkeypatch.setattr(readback, "_run_over_ssh", ssh)
    monkeypatch.setattr(
        readback, "_resolve_ssh_targets", AsyncMock(return_value=[("host", "key", None)])
    )
    monkeypatch.setattr(
        readback,
        "DockerRegistryClient",
        lambda: SimpleNamespace(manifest_digest=AsyncMock(return_value="abc")),
    )
    component = {
        "name": manifest["name"],
        "module": "fictional_module",
        "distribution": "fictional-dist",
    }
    if conflict:
        with pytest.raises(EnvContractMergeError, match="KEY"):
            await readback.read_deployment("product", "target", component=component)
        return
    deployed = await readback.read_deployment("product", "target", component=component)
    backend = deployed["backend"]["component"]
    proof = issuance_proof("project", backend["manifest"], backend["contract"], admin)
    assert proof["key_ids"] == ["safe-id"]
    assert backend["active"] == identity
    assert backend["version"] == "1.0.0"
    assert backend["contract"]["entries"]["APP_NAME"]["source"] == "derived"
    assert backend["contract"]["entries"]["KEY"]["source"] == "platform_key"
    if moved_pin:
        assert (
            backend["contract"]["entries"]["APP_NAME"]["description"]
            == "Candidate fixture declaration"
        )


def test_bot_binding_readback_runs_without_backend_yaml_dependency(tmp_path, monkeypatch):
    from shared.live_harness_mechanical_readback import COMPONENT_READ

    binding = tmp_path / "services/tg_bot/bindings/opaque-module.yaml"
    binding.parent.mkdir(parents=True)
    binding.write_text("binding bytes")
    real_import = builtins.__import__

    def bot_import(name, *args, **kwargs):
        if name == "yaml":
            raise ModuleNotFoundError("bot runtime has no YAML parser")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", bot_import)
    result = {}
    exec(  # noqa: S102 - execute the actual container probe
        COMPONENT_READ,
        {
            "root": tmp_path,
            "component": {"name": "opaque-module"},
            "result": result,
            "hashlib": hashlib,
        },
    )
    assert result["component"]["binding_sha256"] == hashlib.sha256(binding.read_bytes()).hexdigest()


async def test_post_merge_readback_uses_pr_and_commit_without_requiring_a_live_branch(monkeypatch):
    from shared import live_harness_platform as readback

    manifest, contract, admin = facts()
    monkeypatch.setenv("LIVE_CONTOUR", "stand")
    monkeypatch.setenv("PLATFORM_AUTH_ADMIN_URL", "http://fake")
    monkeypatch.setenv("PLATFORM_AUTH_ADMIN_TOKEN", "private-admin")
    monkeypatch.setattr(
        readback,
        "read_deployment",
        AsyncMock(
            return_value={"backend": {"component": {"manifest": manifest, "contract": contract}}}
        ),
    )
    publication = AsyncMock(return_value={"merge_sha": "merge"})
    monkeypatch.setattr(readback, "read_publication", publication)
    monkeypatch.setattr(readback, "api_client", SimpleNamespace(close=AsyncMock()))

    class HTTP:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def get(self, url, **kwargs):
            return SimpleNamespace(is_success=True, json=lambda: admin)

    monkeypatch.setattr(readback.httpx, "AsyncClient", lambda **kwargs: HTTP())
    await readback.invoke(
        SimpleNamespace(
            project="project",
            project_name="product",
            server="target",
            component="{}",
            owner="owner",
            repo="repo",
            base="base",
            head="head",
            story="story",
            merge="merge",
            pr=4,
        )
    )
    publication.assert_awaited_once_with("owner", "repo", "base", "head", merge="merge", pr=4)


def facts():
    sources = {
        "KEY": {
            "kind": "platform_key",
            "service": "catalog-service",
            "scopes": ["read"],
            "quota": {"items": 50},
        },
        "URL": {
            "kind": "platform_base_url",
            "service": "catalog-service",
            "url": "https://platform.example/service",
        },
    }
    manifest = {"environment": [{"name": key, "source": value} for key, value in sources.items()]}
    contract = {
        "entries": {
            key: {"source": value["kind"], **{k: v for k, v in value.items() if k != "kind"}}
            for key, value in sources.items()
        }
    }
    admin = {
        "orchestrator_project_id": "project",
        "disabled": False,
        "grants": {"catalog-service": {"scopes": ["read"], "quota": {"items": 50}}},
        "keys": [{"key_id": "safe-id", "revoked_at": None}],
    }
    return manifest, contract, admin


def test_proof_keeps_ids_and_hashes():
    proof = issuance_proof("project", *facts())
    assert proof["key_ids"] == ["safe-id"]
    assert len(proof["grant_sha256"]) == 64


@pytest.mark.parametrize("change", ["project", "scopes", "quota", "key", "source", "disabled"])
def test_mismatched_or_revoked_issuance_fails(change):
    manifest, contract, admin = deepcopy(facts())
    if change == "project":
        admin["orchestrator_project_id"] = "foreign"
    elif change in {"scopes", "quota"}:
        admin["grants"]["catalog-service"][change] = [] if change == "scopes" else {}
    elif change == "key":
        admin["keys"][0]["revoked_at"] = "2026-01-01"
    elif change == "source":
        contract["entries"]["KEY"]["source"] = "user_secret"
    else:
        admin["disabled"] = True
    with pytest.raises(ValueError):
        issuance_proof("project", manifest, contract, admin)
