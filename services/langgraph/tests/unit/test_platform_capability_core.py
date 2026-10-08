"""Capability manifest core references follow the installed pinned kit tooling."""

from framework.spec.package_resolution import CORE_VERSION
import pytest
import yaml

from scripts import platform_capabilities


@pytest.mark.parametrize("core_version", [CORE_VERSION, "9.8.7"])
def test_manifest_resolves_core_from_pinned_tooling(tmp_path, monkeypatch, core_version):
    monkeypatch.setattr(platform_capabilities, "CORE_VERSION", core_version)
    path = tmp_path / "capabilities.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "status": "draft",
                "review": "Test fixture",
                "derived_from": {
                    "kit": {
                        "source": "gh:fictional/kit",
                        "commit": "0" * 40,
                        "note": "Pinned kit",
                    },
                    "code": ["service.py"],
                },
                "can": [
                    {
                        "id": "catalog_install",
                        "name": "Install",
                        "plain": "Install a capability",
                        "how": "core-{core_version} at {server_ip}:{port}",
                    }
                ],
                "cannot": [
                    {
                        "id": "example",
                        "name": "Example",
                        "plain": "Unavailable",
                        "why": "Not supported",
                        "technical": "Not supported",
                        "detect": ["example"],
                    }
                ],
                "kit": {
                    "modules": [{"name": "backend", "plain": "Backend"}],
                    "core": [],
                    "catalog": "Live catalog",
                },
                "deploy_targets": [
                    {
                        "service": "backend",
                        "requestable": True,
                        "http_health": True,
                        "exposure": "public",
                    }
                ],
                "secret_kinds": [{"source": "literal", "plain": "Literal"}],
                "derived_keys": [{"key": "KEY", "value": "value"}],
                "kit_derived_keys_not_resolved": [],
            }
        )
    )

    manifest = platform_capabilities.load_manifest(path)

    assert manifest.can[0].how == f"core-{core_version} at {{server_ip}}:{{port}}"
