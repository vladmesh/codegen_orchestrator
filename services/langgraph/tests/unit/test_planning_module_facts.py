"""Planning facts belong to one attempt and the complete snapshot it verified."""

from copy import deepcopy
from dataclasses import replace
from unittest.mock import Mock

import pytest
import yaml

from src import catalog_install, kit_catalog, planning_module_jobs
from src.catalog_install import INSTALL_PYTHON_VERSION, InstallRefusal
from src.kit_catalog import install_snapshot
from src.planning_module_jobs import PlanningModuleFacts, module_job_contracts, plan_module_install


def test_equal_injected_snapshots_resolve_once_without_sharing_mutable_results(
    activated_kit_catalog, monkeypatch
):
    parse = Mock(wraps=kit_catalog.parse_catalog)
    resolve = Mock(wraps=planning_module_jobs.resolve_install_payload)
    monkeypatch.setattr(kit_catalog, "parse_catalog", parse)
    monkeypatch.setattr(planning_module_jobs, "resolve_install_payload", resolve)
    facts = PlanningModuleFacts()
    injected = install_snapshot(activated_kit_catalog)
    catalog = facts.restore(injected)
    install, jobs = plan_module_install(catalog, "reminders", INSTALL_PYTHON_VERSION, facts=facts)
    original = deepcopy(jobs)
    jobs["reminders"]["timers"].clear()
    install.binding.functions.clear()

    catalog = facts.restore(deepcopy(injected))
    current, jobs = plan_module_install(catalog, "reminders", INSTALL_PYTHON_VERSION, facts=facts)
    assert current.binding.functions == ["textparse.when"]
    assert jobs == original
    assert module_job_contracts(catalog, [current] * 16, facts=facts) == original
    assert parse.call_count == 1
    assert resolve.call_count == 1


@pytest.mark.parametrize("changed", ["catalog", "binding", "manifest", "schema"])
def test_mutated_injected_bytes_are_verified_before_reuse(activated_kit_catalog, changed):
    facts = PlanningModuleFacts()
    injected = deepcopy(install_snapshot(activated_kit_catalog))
    catalog = facts.restore(injected)
    plan_module_install(catalog, "reminders", INSTALL_PYTHON_VERSION, facts=facts)
    if changed == "catalog":
        injected["catalog"] += "\n# changed\n"
        reason = "catalog_provenance_mismatch"
    elif changed == "binding":
        injected["bindings"]["reminders"] = injected["bindings"]["reminders"].replace(
            "textparse.when", "invented.when"
        )
        reason = "binding_dependency_missing"
    else:
        manifest = yaml.safe_load(injected["manifests"]["reminders"])
        if changed == "manifest":
            manifest["name"] = "invented"
            reason = "binding_manifest_identity_mismatch"
        else:
            manifest["jobs_schema"]["properties"]["tick"] = {"type": "invented"}
            reason = "invalid_"
        injected["manifests"]["reminders"] = yaml.safe_dump(manifest)
    with pytest.raises(ValueError, match=reason):
        catalog = facts.restore(injected)
        plan_module_install(catalog, "reminders", INSTALL_PYTHON_VERSION, facts=facts)


def test_mutated_manifest_replaces_jobs_even_when_install_closure_is_equal(activated_kit_catalog):
    facts = PlanningModuleFacts()
    catalog = deepcopy(activated_kit_catalog)
    install, jobs = plan_module_install(catalog, "reminders", INSTALL_PYTHON_VERSION, facts=facts)
    assert jobs["reminders"]["timers"] == {"reminders.tick": 60}
    manifest = yaml.safe_load(catalog.manifests["reminders"])
    manifest["timers"][0]["every_seconds"] = 120
    catalog.manifests["reminders"] = yaml.safe_dump(manifest)

    changed = module_job_contracts(catalog, [install], facts=facts)
    assert changed["reminders"]["timers"] == {"reminders.tick": 120}
    assert changed["reminders"]["manifest_sha256"] != jobs["reminders"]["manifest_sha256"]


@pytest.mark.parametrize("identity", ["commit", "core_version", "python_version"])
def test_changed_resolution_identity_does_not_acquire_old_facts(
    activated_kit_catalog, identity, monkeypatch
):
    resolve = Mock(wraps=planning_module_jobs.resolve_install_payload)
    monkeypatch.setattr(planning_module_jobs, "resolve_install_payload", resolve)
    facts = PlanningModuleFacts()
    catalog = activated_kit_catalog
    python = INSTALL_PYTHON_VERSION
    install, _ = plan_module_install(catalog, "reminders", python, facts=facts)
    if identity == "python_version":
        python = "3.11.0"
    else:
        catalog = replace(
            catalog, **{identity: {"commit": "c" * 40, "core_version": "2.4.0"}[identity]}
        )
    current, jobs = plan_module_install(catalog, "reminders", python, facts=facts)
    assert current != install
    assert resolve.call_count == 2
    assert jobs["reminders"]["catalog"] == current.catalog.model_dump(mode="json")
    if identity == "python_version":
        assert module_job_contracts(catalog, [install, current], facts=facts) == jobs
    else:
        with pytest.raises(InstallRefusal, match="plan_drift"):
            module_job_contracts(catalog, [install, current], facts=facts)


def test_changed_repository_is_validated_before_reuse(activated_kit_catalog):
    facts = PlanningModuleFacts()
    plan_module_install(activated_kit_catalog, "reminders", INSTALL_PYTHON_VERSION, facts=facts)
    with pytest.raises(ValueError, match="repository"):
        plan_module_install(
            replace(activated_kit_catalog, repository="other/kit"),
            "reminders",
            INSTALL_PYTHON_VERSION,
            facts=facts,
        )


def test_later_duplicate_drift_wins_over_verified_facts(activated_kit_catalog):
    facts = PlanningModuleFacts()
    install, _ = plan_module_install(
        activated_kit_catalog, "tg-channels", INSTALL_PYTHON_VERSION, facts=facts
    )
    drifted = install.model_copy(update={"catalog_digest": "0" * 64})
    with pytest.raises(InstallRefusal, match="plan_drift: tg-channels"):
        module_job_contracts(activated_kit_catalog, [install, drifted], facts=facts)


def test_host_tooling_change_is_not_reused(activated_kit_catalog, monkeypatch):
    facts = PlanningModuleFacts()
    install, _ = plan_module_install(
        activated_kit_catalog, "reminders", INSTALL_PYTHON_VERSION, facts=facts
    )
    monkeypatch.setattr(catalog_install, "installed_tooling_commit", lambda: "c" * 40)
    with pytest.raises(InstallRefusal, match="plan_drift"):
        module_job_contracts(activated_kit_catalog, [install], facts=facts)


def test_a_new_attempt_verifies_the_same_snapshot_again(activated_kit_catalog, monkeypatch):
    resolve = Mock(wraps=planning_module_jobs.resolve_install_payload)
    monkeypatch.setattr(planning_module_jobs, "resolve_install_payload", resolve)
    for _ in range(2):
        plan_module_install(
            activated_kit_catalog,
            "reminders",
            INSTALL_PYTHON_VERSION,
            facts=PlanningModuleFacts(),
        )
    assert resolve.call_count == 2


def test_briefing_reflects_mutated_manifest_bytes(activated_kit_catalog, monkeypatch):
    from src.consumers import architect

    monkeypatch.setattr(architect, "_rendered_kit_catalog", None)
    catalog = deepcopy(activated_kit_catalog)
    assert '"reminders.tick": 60' in architect._kit_catalog_briefing(catalog)
    manifest = yaml.safe_load(catalog.manifests["reminders"])
    manifest["timers"][0]["every_seconds"] = 120
    catalog.manifests["reminders"] = yaml.safe_dump(manifest)
    assert '"reminders.tick": 120' in architect._kit_catalog_briefing(catalog)
