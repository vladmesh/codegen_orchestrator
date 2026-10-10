"""`kit check-install --json` result version 1, admitted only as the kit defines it."""

from pydantic import ValidationError
import pytest

from shared.contracts.dto.catalog_install import (
    CatalogInstall,
    InstallPreflight,
    product_glue_handoff,
)

INSTALL = CatalogInstall.model_validate(
    {
        "package": {
            "name": "reminders",
            "distribution": "codegen-kit-reminders",
            "version": "0.5.0",
            "tag": "packages/reminders/v0.5.0",
        },
        "libraries": [
            {
                "name": "textparse",
                "distribution": "codegen-kit-textparse",
                "version": "0.1.0",
                "tag": "packages/textparse/v0.1.0",
            }
        ],
        "binding": {
            "package": "reminders",
            "resource": "codegen_kit_reminders:bindings/default.yaml",
            "sha256": "a" * 64,
            "functions": ["textparse.when"],
        },
        "core_version": "2.5.0",
        "python_version": "3.12.0",
        "catalog_digest": "b" * 64,
        "tooling_commit": "c" * 40,
        "catalog": {
            "repository": "https://github.com/vladmesh/codegen-product-kit.git",
            "commit": "d" * 40,
            "catalog_sha256": "e" * 64,
        },
    }
)


def item(code, owner="product", symbol=None, line=4):
    return {
        "code": code,
        "path": "services/tg_bot/src/commands.py",
        "line": line,
        "owner": owner,
        "symbol": symbol,
        "key": None,
        "command": "remind",
        "conflict": "/remind is registered twice",
        "action": "rename the product's /remind",
        "other": {"owner": "package:reminders", "path": None, "line": None, "symbol": None},
    }


def result(status="mechanical", glue=(), incompatible=None, **target):
    return {
        "result_version": 1,
        "package": "reminders",
        "status": status,
        "product_core": "2.5.0",
        "target": {
            "route": "catalog",
            "catalog_source": "https://github.com/vladmesh/codegen-product-kit.git",
            "catalog_ref": "d" * 40,
            "tag": "packages/reminders/v0.5.0",
            "version": "0.5.0",
            "requires_core": ">=2.4,<3",
            "metadata_sha256": "f" * 64,
        }
        | target,
        "glue": list(glue),
        "incompatible": incompatible,
    }


@pytest.mark.parametrize(
    "answer",
    [
        result("glue"),
        result("mechanical", [item("command_conflict")]),
        result("incompatible"),
        result("mechanical", incompatible={"code": "core_range", "explanation": "x"}),
        result() | {"result_version": 2},
        result() | {"target": None},
        result(route="package_source"),
        result() | {"unexpected": True},
    ],
)
def test_a_result_whose_status_list_and_reason_disagree_is_not_admitted(answer):
    with pytest.raises(ValidationError):
        InstallPreflight.model_validate(answer)


@pytest.mark.parametrize(
    ("change", "field"),
    [
        ({"catalog_ref": "0" * 40}, "target.catalog_ref"),
        ({"tag": "packages/reminders/v0.4.0"}, "target.tag"),
        ({"version": "0.4.0"}, "target.version"),
        ({"catalog_source": "https://example.com/kit.git"}, "target.catalog_source"),
    ],
)
def test_a_result_for_another_release_names_what_differs(change, field):
    assert InstallPreflight.model_validate(result(**change)).provenance_mismatch(INSTALL) == field
    assert InstallPreflight.model_validate(result()).provenance_mismatch(INSTALL) is None


def test_the_closures_library_is_installer_work_and_everything_else_is_glue():
    own = item("library_required", owner="package:reminders", symbol="textparse")
    other = item("library_required", owner="package:reminders", symbol="dateparse")
    conflict = item("command_conflict", symbol="handle_remind")
    answer = InstallPreflight.model_validate(result("glue", [own, other, conflict]))
    assert [found.symbol for found in answer.outstanding_glue(INSTALL)] == [
        "dateparse",
        "handle_remind",
    ]
    assert InstallPreflight.model_validate(result("glue", [own])).outstanding_glue(INSTALL) == []


def test_only_product_owned_glue_becomes_a_concrete_repair():
    conflict = InstallPreflight.model_validate(
        result("glue", [item("command_conflict", symbol="handle_remind")])
    )
    text = product_glue_handoff(conflict, INSTALL)
    assert "reminders 0.5.0 (packages/reminders/v0.5.0, catalog " + "d" * 40 in text
    assert (
        "- command_conflict at services/tg_bot/src/commands.py:4 (handle_remind): "
        "/remind is registered twice. Do: rename the product's /remind"
    ) in text
    package_owned = InstallPreflight.model_validate(
        result("glue", [item("library_required", owner="package:reminders", symbol="dateparse")])
    )
    assert product_glue_handoff(package_owned, INSTALL) is None
    assert product_glue_handoff(InstallPreflight.model_validate(result()), INSTALL) is None
