"""Require passed, SHA-bound runner evidence of the activated snapshot and the stored plan.

The checks of the kit's own runner workflow for a published release, applied to the
evidence `tests/runner/activated_snapshot_proof.py` wrote, plus what this orchestrator's
contract adds: the catalog is the activated commit of `shared/catalog_activation.yaml`
before and after the installs, every installed payload is the INSTALL task the production
path persisted from the confirmed brief's stored plan, and no model chose any of it.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

import yaml


def verify(evidence: dict, args: argparse.Namespace, activation: dict, support) -> dict:  # noqa: C901, PLR0912, PLR0915 - one ordered evidence contract
    pinned = {
        "kit": args.kit_sha,
        "orchestrator": args.orchestrator_sha,
        "platform": args.platform_sha,
    }
    packages = args.packages.split(",")
    assert evidence["status"] == "passed", evidence.get("error")
    assert evidence["pinned"] == pinned, evidence["pinned"]
    assert pinned["kit"] == activation["commit"] == activation["tooling_commit"], activation
    assert evidence["revisions"]["product_tooling"]["vcs_info"]["commit_id"] == pinned["kit"]
    assert evidence["template"]["resolved_sha"] == pinned["kit"]
    for key in (
        "install",
        "release",
        "coexistence",
        "product_ci",
        "cold_main_regression",
        "main_images",
        "integration_without_host_environments",
        "platform",
        "deployment",
        "scenario",
        "catalog",
        "production_plan",
        "operations",
        "confirmed_settings",
    ):
        assert evidence.get(key), f"missing evidence section {key}"
    release, catalog, production = (
        evidence["release"],
        evidence["catalog"],
        evidence["production_plan"],
    )
    assert evidence["proof_mode"] == release["mode"] == "published_release", release["mode"]
    # The activated immutable snapshot, never the default branch or a fixture.
    assert evidence["catalog_mode"] == catalog["mode"] == "activated_snapshot", catalog["mode"]
    assert catalog["prospective"] is False and "http_requests" not in catalog, catalog
    for key in ("commit", "catalog_sha256", "catalog_digest"):
        assert catalog[key] == activation[key] == catalog["after_install"][key], key
    assert catalog["activation"] == activation
    # Planner, payloads and both probes reached the one activated catalog.
    assert set(catalog["planner_digests"].values()) == {activation["catalog_digest"]}, catalog
    assert release["planner_catalog_digest"] == activation["catalog_digest"], release
    assert all(
        item == {"preflight_returncode": 0, "readback": True}
        for item in catalog["probe_agreement"].values()
    ), catalog
    # What was installed is what the production path persisted from the stored plan.
    assert production["activation"] == activation and production["model_channels"] == []
    assert [route["route"] for route in production["preview"]["routes"]] == ["module"] * len(
        packages
    )
    # The first request named no id; its words alone resolved the module it installs.
    assert [route["capability_id"] for route in production["preview"]["routes"]] == [
        item["capability_id"] for item in production["plan"]["capabilities"]
    ] and all(route["capability_id"] for route in production["preview"]["routes"])
    stored = {
        item["install"]["package"]["name"]: item["install"]
        for item in production["plan"]["capabilities"]
        if item["install"]
    }
    persisted = {name: task["install"] for name, task in production["install_tasks"].items()}
    installed = evidence["install_payloads"]
    assert sorted(stored) == sorted(persisted) == sorted(installed) == sorted(packages), (
        sorted(stored),
        sorted(persisted),
        sorted(installed),
    )
    for name in packages:
        assert stored[name] == persisted[name] == installed[name], name
        assert installed[name]["catalog"] == {
            "repository": activation["repository"],
            "commit": activation["commit"],
            "catalog_sha256": activation["catalog_sha256"],
        }, installed[name]["catalog"]
    # A new owner's draft order: planned before any scaffold, released by the scaffold.
    assert production["project_status_at_planning"] == "draft", production
    assert production["repository"]["git_url"].startswith("pending://"), production
    assert production["admission_before_scaffold"]["reason"] == "workspace_not_ready", production
    scaffold = production["scaffold"]
    assert scaffold["recorded_by"] == "scaffolder.src.consumer._update_project_on_success"
    assert scaffold["project_status"] == "active" and scaffold["workspace_ready"] is True
    assert scaffold["service_template"]["commit"] == str(evidence["template"]["commit"]), scaffold
    # Each install was an admitted, claimed operation in its own checkout, under the kit's
    # read-only preflight of the exact persisted release.
    operations = evidence["operations"]
    assert sorted(operations) == sorted(packages), sorted(operations)
    for name in packages:
        operation = operations[name]
        assert operation["task_id"] == production["install_tasks"][name]["task_id"], name
        assert operation["task_status"] == "done" and operation["state"] == "published", name
        assert operation["checkout"] == (
            f"{production['repository']['id']}/{operation['operation_id']}"
        ), operation
        assert operation["checkout_removed"] is True, operation
        assert operation["head_sha"] == evidence["installs"][name]["head_sha"], name
        preflight = operation["preflight"]
        assert preflight["result_version"] == 1 and preflight["status"] in {"mechanical", "glue"}
        assert preflight["target"]["route"] == "catalog", preflight
        assert preflight["target"]["catalog_ref"] == activation["commit"], preflight
        assert preflight["target"]["tag"] == installed[name]["package"]["tag"], preflight
        assert preflight["target"]["version"] == installed[name]["package"]["version"]
        closure = {item["name"] for item in installed[name]["libraries"]}
        for item in preflight["glue"]:
            # Only the closure's own library request; any product glue would have refused.
            assert item["code"] == "library_required" and item["symbol"] in closure, item
    assert len({operation["checkout"] for operation in operations.values()}) == len(packages)
    # The confirmed answers, written and read back through the production boundary.
    confirmed = evidence["confirmed_settings"]
    assert confirmed["brief_id"] == production["brief_id"], confirmed
    values = {item["key"]: item["value"] for item in confirmed["settings"]}
    planned = {item["key"]: item["value"] for item in production["plan"]["settings"]}
    assert all(values[key] == value for key, value in planned.items()), (values, planned)
    assert values["tg_channels.starting_channels"] == production["initial_channels"], values
    assert production["initial_channels"], production
    assert values.get("language") in support.LANGUAGES, values
    assert all(item["written"] for item in confirmed["seed"]), confirmed["seed"]
    assert confirmed["readback"] is None, confirmed["readback"]
    assert evidence["matrix"]["packages"] == packages, evidence["matrix"]
    assert sorted(evidence["installs"]) == sorted(packages), sorted(evidence["installs"])
    assert evidence["module"]["version"] == release["version"] == args.package_version, release
    assert release["probe_source"]["target"] == release["tag_target"]
    # The pinned published tag, read from the real remote before planning; the probe fetched
    # exactly it, and no tag was created anywhere.
    pin = support.published_release("tg-channels", args.package_version)
    assert release["published"] is True and "intended_tag" not in release, release
    assert release["published_tag"] == {
        "tag": pin.tag,
        "type": "tag",
        "tag_object": pin.tag_object,
        "target": pin.target,
        "tree": pin.tree,
    }, release
    source = release["probe_source"]
    assert (source["tag"], source["tag_object"], source["target"], source["tree"]) == (
        pin.tag,
        pin.tag_object,
        pin.target,
        pin.tree,
    ), source
    assert release["remote_package_tags"][pin.tag] == pin.tag_object
    assert release["installed_distribution"]["version"] == args.package_version, release
    scenario = evidence["scenario"]
    assert scenario["delivered_post"]["text"] and scenario["delivered_post"]["chat_id"]
    # Grant, revoke, grant again: each 200 is what the next ordinary access read returns.
    cycle = scenario["access_acknowledgments"]["steps"]
    assert [(step["operation"], step["expected"]) for step in cycle] == [
        ("grant", "active"),
        ("revoke", "inactive"),
        ("grant", "active"),
    ], cycle
    for step in cycle:
        assert step["write"]["status"] == step["access"]["status"] == 200, step
        assert step["write"]["body"]["status"] == step["expected"], step
        assert step["access"]["body"] == step["write"]["body"], step
    if "reminders" in packages:
        assert scenario["reminders"]["delivered"]["text"].startswith("Reminder: ")
    languages = scenario["languages"]
    assert not languages["ledger"]["duplicates"] and not languages["ledger"]["unmatched"]
    for locale in support.LANGUAGES:
        phase = languages[locale]
        assert phase["readback"]["body"]["value"] == locale, phase["readback"]
        assert [probe["kind"] for probe in phase["probes"].values()] == [
            kind for kind, _ in support.LANGUAGE_PROBES
        ]
        for kind, probe in phase["probes"].items():
            assert probe["chat_id"] == support.probe_chat(locale, kind), probe
            assert probe["reply"]["chat_id"] == probe["chat_id"], probe
            assert probe["reply"]["seq"] > probe["watermark"], probe
            assert isinstance(probe["update_id"], int) and probe["problems"] == [], probe
    return {
        "catalog_mode": catalog["mode"],
        "catalog_commit": catalog["commit"],
        "remote_head_at_start": catalog["remote_head_at_start"]["commit"],
        "packages": packages,
        "brief_id": production["brief_id"],
        "install_tasks": {
            name: task["task_id"] for name, task in production["install_tasks"].items()
        },
        "operations": {
            name: {key: operation[key] for key in ("operation_id", "checkout", "head_sha")}
            | {"preflight": operation["preflight"]["status"]}
            for name, operation in operations.items()
        },
        "confirmed_settings": values,
        "delivered_post": scenario["delivered_post"],
        "access_acknowledgments": [
            (step["operation"], step["access"]["body"]["status"]) for step in cycle
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--kit-dir", type=Path, required=True)
    parser.add_argument("--activation", type=Path, required=True)
    for name in ("kit", "orchestrator", "platform"):
        parser.add_argument(f"--{name}-sha", required=True)
    parser.add_argument("--packages", required=True)
    parser.add_argument("--package-version", required=True)
    args = parser.parse_args()
    sys.path.insert(0, str(args.kit_dir / "tests/runner"))
    import support  # noqa: PLC0415 - the pinned kit's own evidence helpers

    summary = verify(
        json.loads(args.evidence.read_text()),
        args,
        yaml.safe_load(args.activation.read_text()),
        support,
    )
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
