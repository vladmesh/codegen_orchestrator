"""A local source probe's evidence is its own strict type, compared in one fixed order."""

from pydantic import ValidationError
import pytest

from shared.contracts.dto.catalog_install import InstallVerification
from shared.contracts.dto.local_install import (
    LocalInstallVerification,
    LocalPackageInstall,
    local_verification_mismatch,
)
from shared.tests.unit.test_local_install import local_install

INSTALL = LocalPackageInstall.model_validate(local_install())
SOURCE = {
    "tree": "3" * 40,
    "entry_point": "notes_digest:package",
    "metadata_sha256": "4" * 64,
    "requires_core": ">=2,<3",
    "requires_python": ">=3.11",
    "default_binding": None,
    "binding_sha256": None,
}
INSTALLED = {
    "wheel": "services/backend/packages/codegen_kit_notes_digest-0.1.0-py3-none-any.whl",
    "wheel_sha256": "5" * 64,
    "wheel_metadata_sha256": "4" * 64,
    "lock_sha256": "5" * 64,
    "direct_url": "file:///work/services/backend/packages/codegen_kit_notes_digest-0.1.0-py3-none-any.whl",
    "version": "0.1.0",
    "files_compared": 3,
    "manifest_sha256": "6" * 64,
    "binding_sha256": None,
}


def verification(mode="provenance", target=None, source=None, installed=None, **changes):
    return {
        "mode": mode,
        "target": target or local_install(),
        "head": "1" * 40,
        "core_version": "2.2.0",
        "python_version": "3.12.0",
        "tooling_commit": "2" * 40,
        "source": SOURCE | (source or {}),
        "residue": [],
        "installed": None if mode == "provenance" else INSTALLED | (installed or {}),
        "protected_sha256": {"pyproject.toml": "7" * 64},
        **changes,
    }


PROVENANCE = LocalInstallVerification.model_validate(verification())


def test_a_provenance_and_its_readback_are_the_saved_targets_evidence():
    readback = LocalInstallVerification.model_validate(
        verification("readback", residue=["packages/notes_digest/build"], python_version="3.12.11")
    )
    assert local_verification_mismatch(PROVENANCE, INSTALL) is None
    assert local_verification_mismatch(readback, INSTALL, PROVENANCE) is None
    assert readback.target == INSTALL


@pytest.mark.parametrize(
    ("changes", "field"),
    [
        (
            {"target": local_install(project_id="00000000-0000-0000-0000-0000000000b2")},
            "target.project_id",
        ),
        ({"target": local_install(repository_id="repo-2")}, "target.repository_id"),
        ({"target": local_install(commit="8" * 40), "head": "8" * 40}, "target.commit"),
        ({"target": local_install(path="packages/other")}, "target.path"),
        (
            {"target": local_install(package=local_install()["package"] | {"version": "0.1.1"})},
            "target.package",
        ),
        ({"head": "8" * 40}, "head"),
        ({"core_version": "2.3.0"}, "core_version"),
        ({"tooling_commit": "9" * 40}, "tooling_commit"),
    ],
)
def test_a_provenance_for_another_target_or_checkout_names_the_first_difference(changes, field):
    found = LocalInstallVerification.model_validate(verification(**changes))
    assert local_verification_mismatch(found, INSTALL) == field


@pytest.mark.parametrize(
    ("changes", "field"),
    [
        ({"source": {"tree": "8" * 40}}, "source"),
        ({"protected_sha256": {"pyproject.toml": "8" * 64}}, "protected_sha256"),
        ({"installed": {"wheel_metadata_sha256": "8" * 64}}, "installed.wheel_metadata_sha256"),
        ({"installed": {"lock_sha256": "8" * 64}}, "installed.lock_sha256"),
        ({"installed": {"version": "0.1.1"}}, "installed.version"),
        ({"installed": {"binding_sha256": "8" * 64}}, "installed.binding_sha256"),
    ],
)
def test_a_readback_that_is_not_its_provenances_install_names_the_difference(changes, field):
    found = LocalInstallVerification.model_validate(verification("readback", **changes))
    assert local_verification_mismatch(found, INSTALL, PROVENANCE) == field


def test_a_readback_is_compared_only_against_a_provenance():
    readback = LocalInstallVerification.model_validate(verification("readback"))
    assert local_verification_mismatch(readback, INSTALL) == "provenance"
    assert local_verification_mismatch(readback, INSTALL, readback) == "provenance"


@pytest.mark.parametrize(
    "payload",
    [
        verification() | {"installed": INSTALLED},
        verification("readback") | {"installed": None},
        verification(residue=["packages/notes_digest/extra.py"]),
        verification(source={"binding_sha256": "8" * 64}),
        verification(source={"default_binding": "notes_digest:bindings/default.yaml"}),
        verification(residue=["/etc/passwd"], mode="readback", installed=INSTALLED),
        verification("readback", installed={"wheel": "../outside.whl"}),
        verification("readback", installed={"direct_url": "https://example.com/x.whl"}),
        verification("readback", installed={"files_compared": 0}),
        verification(component_targets={"notes-digest": "8" * 40}),
        verification(target=local_install() | {"tag": "packages/notes-digest/v0.1.0"}),
    ],
    ids=[
        "provenance-with-install",
        "readback-without-install",
        "provenance-with-residue",
        "binding-digest-without-binding",
        "binding-without-digest",
        "residue-outside-product",
        "wheel-outside-product",
        "remote-artifact",
        "nothing-compared",
        "catalog-component-targets",
        "catalog-tag",
    ],
)
def test_evidence_of_another_shape_is_refused(payload):
    with pytest.raises(ValidationError):
        LocalInstallVerification.model_validate(payload)


def test_a_released_install_verification_is_never_local_evidence():
    released = {
        "core_version": "2.2.0",
        "tooling_commit": "2" * 40,
        "binding_sha256": "4" * 64,
        "distributions": {"notes-digest": "0.1.0"},
        "component_targets": {"notes-digest": "3" * 40},
        "protected_sha256": {},
    }
    InstallVerification.model_validate(released)
    with pytest.raises(ValidationError):
        LocalInstallVerification.model_validate(released)
    with pytest.raises(ValidationError):
        InstallVerification.model_validate(PROVENANCE.model_dump(mode="json"))
