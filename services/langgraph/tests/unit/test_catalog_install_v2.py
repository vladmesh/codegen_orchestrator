"""The same planner admits finite bilingual bindings without parse functions."""

from dataclasses import replace
import hashlib
from importlib.metadata import distribution
import json
from pathlib import Path

from framework.catalog import parse_catalog
import pytest

from src.catalog_install import InstallRefusal, plan_install_payload
from src.consumers.architect import _kit_catalog_briefing
from src.kit_catalog import installable

DATA = Path(__file__).parent / "fixtures/catalog-install-v2"


def snapshot():
    raw = (DATA / "catalog.yaml").read_text()
    return replace(
        installable(parse_catalog(raw), "fixture"),
        bindings={"notebook": (DATA / "default.yaml").read_text()},
        manifests={"notebook": (DATA / "package.yaml").read_text()},
        raw=raw,
    )


def test_v2_closure_keeps_catalog_identity_and_has_no_parser_functions():
    current = snapshot()
    payload = plan_install_payload(current, "notebook", "3.12.0")
    assert payload.model_dump(mode="json") == {
        "package": {
            "name": "notebook",
            "distribution": "codegen-kit-notebook",
            "version": "0.1.0",
            "tag": "packages/notebook/v0.1.0",
        },
        "libraries": [],
        "binding": {
            "package": "notebook",
            "resource": "codegen_kit_notebook:bindings/default.yaml",
            "sha256": hashlib.sha256(current.bindings["notebook"].encode()).hexdigest(),
            "functions": [],
        },
        "core_version": current.core_version,
        "python_version": "3.12.0",
        "catalog_digest": current.digest,
        "tooling_commit": json.loads(
            distribution("codegen-kit-tooling").read_text("direct_url.json")
        )["vcs_info"]["commit_id"],
    }


def test_catalog_briefing_exposes_binding_owned_setting_keys_and_values():
    briefing = _kit_catalog_briefing(snapshot())
    assert "interface_locale" in briefing
    assert '"enum": ["ru", "en"]' in briefing


@pytest.mark.parametrize(
    "before,after",
    [
        ("name: notebook", "name: other"),
        ("version: 0.1.0", "version: 0.2.0"),
        ("codegen_kit_notebook:bindings/default.yaml", "codegen_kit_notebook:bindings/other.yaml"),
    ],
)
def test_v2_manifest_identity_mismatch_refuses(before, after):
    current = snapshot()
    current = replace(
        current, manifests={"notebook": current.manifests["notebook"].replace(before, after)}
    )
    with pytest.raises(InstallRefusal, match="binding_manifest_identity_mismatch"):
        plan_install_payload(current, "notebook", "3.12.0")


@pytest.mark.parametrize(
    "before,after,reason",
    [
        ("values: [ru, en]", "values: [en, ru]", "invalid_binding:"),
        ("binding_version: 2", "binding_version: 99", "invalid_binding:"),
        ("action: notebook.add", "action: notebook.unknown", "invalid_binding_contract:"),
    ],
)
def test_invalid_v2_or_unknown_version_is_a_named_refusal(before, after, reason):
    current = snapshot()
    current = replace(
        current, bindings={"notebook": current.bindings["notebook"].replace(before, after)}
    )
    with pytest.raises(InstallRefusal, match=reason) as first:
        plan_install_payload(current, "notebook", "3.12.0")
    with pytest.raises(InstallRefusal, match=reason) as repeated:
        plan_install_payload(current, "notebook", "3.12.0")
    assert str(repeated.value) == str(first.value)
