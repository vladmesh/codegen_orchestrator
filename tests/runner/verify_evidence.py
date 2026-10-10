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


def seed_causality_problems(observation: dict, replies: dict) -> list[str]:
    """Why the observed bot behaviour was not caused by the saved language and channels.

    The observer's first reply to `/channel` must be the empty-name answer in the saved
    language, its first reply to `/channels` the first saved channel, the module's own list
    for it exactly the saved channels, and nothing left subscribed afterwards. Each reply is
    the observer chat's first after its own input, whatever it says: nothing is filtered.
    """
    problems = []
    expected = observation["expected"]
    observer = observation["observer"]
    if observation["grant"]["status"] != 200:
        problems.append(f"the observer was not granted: {observation['grant']}")
    for kind, text in (
        ("language", replies[expected["language"]]["channel"]),
        ("channels", f"@{expected['channels'][0]}" if expected["channels"] else None),
    ):
        record = observation.get(kind)
        reply = None if record is None else record.get("reply")
        if not reply or reply.get("chat_id") != observer or reply["seq"] <= record["watermark"]:
            problems.append(f"no {kind} reply in the observer chat after its input")
        elif text is None or reply.get("text") != text:
            problems.append(f"the first {kind} reply is {reply.get('text')!r}, not {text!r}")
    listed = observation.get("module_list", {})
    channels = sorted(item.get("channel") for item in listed.get("body") or [])
    if listed.get("status") != 200 or channels != sorted(expected["channels"]):
        problems.append(f"the module lists {listed} for a new user, not {expected['channels']}")
    after = observation.get("cleanup", {}).get("after", {})
    if after.get("status") != 200 or after.get("body") != []:
        problems.append(f"the observer kept a subscription: {after}")
    return problems


def lifecycle_problems(evidence: dict, production: dict) -> list[str]:
    """The native order transitions, bound to this order's own ids, or why they are missing.

    One scheduler scaffold publication delivered to the scaffolder's entrypoint and recorded
    ready, then one dispatch publication and delivery per installed operation; a step whose
    stream entry, delivery or ids do not match is a forged or missing transition.
    """
    problems = []
    steps = evidence.get("lifecycle", {}).get("steps", [])
    scaffolds = [step for step in steps if step["kind"] == "full_scaffold"]
    if len(scaffolds) != 1:
        return [f"{len(scaffolds)} full scaffolds recorded"]
    scaffold = scaffolds[0]
    if (
        scaffold["tick"]["entry_id"] != scaffold["delivery"]["entry_id"]
        or scaffold["delivery"]["result"] != {"status": "success"}
        or scaffold["message"]
        != {
            "project_id": production["project_id"],
            "repository_id": production["repository"]["id"],
            "mode": "full",
        }
        or scaffold["project"]["status"] != "active"
        or scaffold["project"]["workspace_ready"] is not True
        or scaffold["project"]["service_template"]["commit"] != str(evidence["template"]["commit"])
        or not scaffold["repository"]["git_url"].startswith("https://github.com/")
        or not any(call["call"] == "create_repo" for call in scaffold["github_calls"])
    ):
        problems.append(f"the full scaffold transition is not native: {scaffold}")
    if steps.index(scaffold) != 0:
        problems.append("a step preceded the full scaffold")
    installs = [step for step in steps if step["kind"] == "install"]
    for name, operation in evidence.get("operations", {}).items():
        mine = [step for step in installs if step["package"] == name]
        if len(mine) != 1:
            problems.append(f"{name}: {len(mine)} install deliveries")
            continue
        step = mine[0]
        if (
            step["tick"]["entry_id"] != step["delivery"]["entry_id"]
            or step["tick"]["entry_id"] != operation["dispatch_entry_id"]
            or step["delivery"]["entry_id"] != operation["delivery_entry_id"]
            or step["tick"]["operation_id"] != operation["operation_id"]
            or step["delivery"]["result"].get("status") != "success"
            or step["delivery"]["result"].get("operation_id") != operation["operation_id"]
        ):
            problems.append(f"{name}: the install delivery is not native: {step}")
    return problems


def conflict_problems(evidence: dict, production: dict, packages: list[str]) -> list[str]:
    """The kit-classified product conflict, its typed handoff, replay and fresh install."""
    problems = []
    handoffs = evidence.get("conflict_handoff", {})
    target = "tg-channels"
    if target not in packages:
        return problems
    record = handoffs.get(target)
    if record is None:
        return [f"no conflict handoff for {target}"]
    operation = record["operation"]
    glue = operation["preflight"]["glue"]
    codes = {item["code"] for item in glue}
    final = evidence["operations"][target]
    repair = record["repair_task"]
    if (
        record["executed"]
        or record["delivery_result"].get("stage") != "preflight"
        or operation["state"] != "refused"
        or operation["stage"] != "preflight"
        or operation["preflight"]["status"] != "glue"
        or not {"binding_language_owner", "command_collision"} <= codes
        or any(item["owner"] != "product" or not item["path"] for item in glue)
        or operation["preflight"]["target"]["catalog_ref"]
        != production["plan"]["activation"]["commit"]
        or operation["base_sha"] != record["fixture_commit"]
        or record["task_after"]["install_operation"] is not None
        or record["task_after"]["blocked_by_task_id"] != repair["id"]
        or repair["type"] != "fix"
        or repair["created_by"] != "catalog_install_glue"
        or repair["dispatch_admitted"] is not True
        or repair["story_id"] != production["story_id"]
        or not all(item["action"] in repair["description"] for item in glue)
        or record["redelivery"]["result"].get("status") != "skipped"
        or final["operation_id"] == operation["id"]
        or final["base_sha"] != record["repair_commit"]
        or final["checkout"] == operation.get("checkout")
    ):
        problems.append(f"the conflict handoff did not hold: {record} / {final}")
    return problems


#: The stand witness disposition each leg's real install must reach: reminders on a product
#: without textparse answers kit check-install with its own `library_required` (exit 3) and
#: adds the library; tg-channels installs mechanically with no library stage.
WITNESS_PATHS = {
    "reminders": {"status": "glue", "returncode": 3, "library": True},
    "tg-channels": {"status": "mechanical", "returncode": 0, "library": False},
}


def stand_witness_problems(evidence: dict, packages: list[str]) -> list[str]:
    """Why the final stand's witness did not accept each package's actual native install.

    The adapter handed each `run_install` result to `tests/live/install_witness.py` and
    retained its input stages and disposition. They must be the stages the harness recorded
    for the same result; the witness, run again here on them with the operation's persisted
    typed preflight and the installed closure, must accept them with the same disposition;
    and each package must take its expected path through prepare, check-install and library.
    """
    from tests.live.install_witness import WitnessRefused, check_execution  # noqa: PLC0415

    problems = []
    for name in packages:
        operation = evidence.get("operations", {}).get(name)
        witness = (operation or {}).get("stand_witness")
        if not witness or "disposition" not in witness:
            problems.append(f"{name}: no stand witness evidence")
            continue
        if witness["stages"] != evidence.get("installs", {}).get(name, {}).get("stages"):
            problems.append(f"{name}: the witnessed stages are not the executor's recorded stages")
            continue
        try:
            again = check_execution(
                witness["stages"],
                preflight=operation["preflight"],
                install=evidence["install_payloads"][name],
                operation_id=operation["operation_id"],
                story_id=operation["story_id"],
                checkout=operation["checkout"],
                base_sha=operation["base_sha"],
            )
        except WitnessRefused as refusal:
            again = refusal.disposition()
        if again != witness["disposition"] or again["accepted"] is not True:
            problems.append(f"{name}: the stand witness refused: {again}")
            continue
        path = WITNESS_PATHS.get(name)
        libraries = [item["name"] for item in evidence["install_payloads"][name]["libraries"]]
        if path is None or (
            again["stages"][0] != "prepare"
            or again["preflight"]["status"] != path["status"]
            or again["preflight"]["returncode"] != path["returncode"]
            or ("library" in again["stages"]) != path["library"]
            or bool(libraries) != path["library"]
            or again["preflight"]["resolved_by_closure"] != (libraries if path["library"] else [])
        ):
            problems.append(f"{name}: the witnessed install did not take its leg's path: {again}")
    return problems


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
    replay = production["confirmation_replay"]
    assert replay["brief_id"] == production["brief_id"], replay
    assert "already confirmed" in replay["tool"], replay
    steps = lifecycle_problems(evidence, production)
    assert not steps, steps
    # Each install was an admitted, claimed operation in its own checkout, under the kit's
    # read-only preflight of the exact persisted release.
    operations = evidence["operations"]
    assert sorted(operations) == sorted(packages), sorted(operations)
    for name in packages:
        operation = operations[name]
        assert operation["task_id"] == production["install_tasks"][name]["task_id"], name
        assert operation["story_id"] == production["story_id"], operation
        assert operation["project_id"] == production["project_id"], operation
        assert operation["repository_id"] == production["repository"]["id"], operation
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
    assert all(item["written"] for item in confirmed["replay"]), confirmed["replay"]
    assert confirmed["replay_readback"] is None, confirmed["replay_readback"]
    assert confirmed["negatives"]["problems"] == [], confirmed["negatives"]
    assert confirmed["negatives"]["mismatch"] and confirmed["negatives"]["undeclared"]
    # Behaviour caused by the saved answers, observed before the harness writes or subscribes.
    causality = seed_causality_problems(evidence["seed_causality"], support.LANGUAGE_REPLIES)
    assert not causality, causality
    assert evidence["seed_causality"]["expected"] == {
        "language": values["language"],
        "channels": values["tg_channels.starting_channels"],
    }, evidence["seed_causality"]["expected"]
    assert (
        evidence["seed_causality"]["channels"]["reply"]["seq"]
        < evidence["scenario"]["negative_unknown_key"]["reply"]["seq"]
    )
    handoff = conflict_problems(evidence, production, args.packages.split(","))
    assert not handoff, handoff
    witnessed = stand_witness_problems(evidence, packages)
    assert not witnessed, witnessed
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
        "stand_witness": {
            name: operation["stand_witness"]["disposition"]
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
    # The orchestrator tree, for `shared` and the stand witness the evidence must satisfy.
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
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
