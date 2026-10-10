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
        "delivered_post": scenario["delivered_post"],
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
