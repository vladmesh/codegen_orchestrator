"""One operation carries either route's target and proof, and a proof only advances once."""

import pytest

from shared.contracts.dto.catalog_install import CatalogInstall, InstallVerification
from shared.contracts.dto.install_operation import (
    InstallCommand,
    InstallDecision,
    InstallOperation,
    verification_refusal,
)
from shared.contracts.dto.local_install import LocalInstallVerification, LocalPackageInstall
from shared.tests.unit.test_local_install import PROJECT, catalog_install, local_install
from shared.tests.unit.test_local_verification import verification

LOCAL = LocalPackageInstall.model_validate(local_install())
CATALOG = CatalogInstall.model_validate(catalog_install())
PROVENANCE = LocalInstallVerification.model_validate(verification())
READBACK = LocalInstallVerification.model_validate(verification("readback"))
RELEASED = InstallVerification.model_validate(
    {
        "core_version": "2.2.0",
        "tooling_commit": "c" * 40,
        "binding_sha256": "a" * 64,
        "distributions": {"reminders": "0.5.0"},
        "component_targets": {"reminders": "e" * 40},
        "protected_sha256": {},
    }
)


def operation(proof) -> dict:
    return {
        "id": "install-1",
        "project_id": str(PROJECT),
        "task_id": "task-1",
        "story_id": "story-1",
        "repository_id": "repo-1",
        "cycle_started_at": "2026-01-02T00:00:00Z",
        "state": "running",
        "stage": "readback",
        "token": "owner",
        "heartbeat_at": None,
        "head_sha": "9" * 40,
        "base_sha": "1" * 40,
        "detail": None,
        "verification": proof.model_dump(mode="json"),
    }


@pytest.mark.parametrize(
    "proof", [RELEASED, PROVENANCE, READBACK], ids=["released", "provenance", "readback"]
)
def test_stored_operation_json_reads_back_its_own_route_proof(proof):
    stored = operation(proof)
    read = InstallOperation.model_validate(stored)
    assert type(read.verification) is type(proof) and read.verification == proof
    assert read.model_dump(mode="json") == stored
    command = InstallCommand.model_validate(
        {"action": "checkpoint", "verification": stored["verification"]}
    )
    assert command.verification == proof


@pytest.mark.parametrize("install", [CATALOG, LOCAL], ids=["catalog", "local"])
def test_a_decision_returns_the_saved_target_of_either_route(install):
    decision = InstallDecision(outcome="admitted", install=install)
    read = InstallDecision.model_validate(decision.model_dump(mode="json"))
    assert type(read.install) is type(install) and read.install == install


OTHER_PRODUCT = LocalInstallVerification.model_validate(
    verification(target=local_install(project_id="00000000-0000-0000-0000-0000000000b2"))
)
MOVED_SOURCE = LocalInstallVerification.model_validate(
    verification("readback", source={"tree": "8" * 40})
)
OTHER_HEAD = LocalInstallVerification.model_validate(verification(head="8" * 40))
OTHER_READBACK = LocalInstallVerification.model_validate(
    verification("readback", installed={"files_compared": 9})
)


@pytest.mark.parametrize(
    ("current", "proof", "refusal"),
    [
        (None, PROVENANCE, None),
        (PROVENANCE, PROVENANCE, None),
        (PROVENANCE, READBACK, None),
        (READBACK, READBACK, None),
        (None, READBACK, "verification_provenance_missing"),
        (PROVENANCE, MOVED_SOURCE, "verification_mismatch"),
        (None, OTHER_PRODUCT, "verification_mismatch"),
        (None, OTHER_HEAD, "verification_mismatch"),
        (PROVENANCE, OTHER_HEAD, "verification_changed"),
        (READBACK, PROVENANCE, "verification_changed"),
        (READBACK, OTHER_READBACK, "verification_changed"),
        (None, RELEASED, "verification_route_mismatch"),
        (PROVENANCE, RELEASED, "verification_route_mismatch"),
    ],
)
def test_a_local_proof_advances_once_from_its_provenance_to_its_readback(current, proof, refusal):
    assert verification_refusal(current, proof, LOCAL) == refusal


@pytest.mark.parametrize(
    ("current", "proof", "refusal"),
    [
        (None, RELEASED, None),
        (RELEASED, RELEASED, None),
        (None, PROVENANCE, "verification_route_mismatch"),
        (None, READBACK, "verification_route_mismatch"),
        (None, RELEASED.model_copy(update={"binding_sha256": "f" * 64}), "verification_mismatch"),
        (
            RELEASED,
            RELEASED.model_copy(update={"component_targets": {"reminders": "f" * 40}}),
            "verification_changed",
        ),
    ],
)
def test_a_catalog_proof_keeps_its_released_meaning(current, proof, refusal):
    assert verification_refusal(current, proof, CATALOG) == refusal
