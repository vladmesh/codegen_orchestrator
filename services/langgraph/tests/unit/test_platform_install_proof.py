"""Issuance evidence is accepted only for the deployed contract's declared grant."""

import builtins
from copy import deepcopy
import hashlib
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from shared.live_harness_platform import issuance_proof


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
