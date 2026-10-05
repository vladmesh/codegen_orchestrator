"""Deterministic closure uses the exact catalog and resource briefed to planning."""

from dataclasses import replace
from pathlib import Path

from framework.catalog import parse_catalog
import pytest

from src.catalog_install import InstallRefusal, plan_install_payload
from src.kit_catalog import installable

DATA = Path(__file__).parent / "fixtures" / "catalog-install"


def snapshot():
    catalog = installable(parse_catalog((DATA / "catalog.yaml").read_text()), "test")
    return replace(
        catalog,
        bindings={"reminders": (DATA / "default.yaml").read_text()},
        manifests={"reminders": (DATA / "package.yaml").read_text()},
    )


def test_closes_reminders_library_and_default_binding_as_one_payload():
    payload = plan_install_payload(snapshot(), "reminders", "3.12.0")
    assert payload.package.name == "reminders"
    assert payload.package.version == "0.5.0"
    assert [item.name for item in payload.libraries] == ["textparse"]
    assert payload.binding.functions == ["textparse.when"]
    assert payload.binding.package == payload.package.name


@pytest.mark.parametrize(
    "name,python,reason",
    [
        ("invented", "3.12.0", "unknown_package"),
        ("reminders", "3.10.0", "incompatible_library"),
    ],
)
def test_refuses_unknown_or_incompatible_before_creating_task(name, python, reason):
    with pytest.raises(InstallRefusal, match=reason):
        plan_install_payload(snapshot(), name, python)


def test_missing_required_binding_resource_is_a_named_refusal():
    with pytest.raises(InstallRefusal, match="binding_unavailable"):
        plan_install_payload(replace(snapshot(), bindings={}), "reminders", "3.12.0")


def test_binding_dependency_cannot_be_replaced_by_an_arbitrary_function():
    current = snapshot()
    corrupt = current.bindings["reminders"].replace("textparse.when", "invented.when")
    with pytest.raises(InstallRefusal, match="binding_dependency_missing"):
        plan_install_payload(
            replace(current, bindings={"reminders": corrupt}), "reminders", "3.12.0"
        )
