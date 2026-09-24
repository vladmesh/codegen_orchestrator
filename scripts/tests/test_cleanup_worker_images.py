"""Tests for the deploy-host worker-image retention boundary."""

from __future__ import annotations

import json
import subprocess

from scripts.cleanup_worker_images import (
    Image,
    cleanup_worker_images,
    main,
    plan_cleanup,
    render_plan,
)
from scripts.tests.fake_docker import FakeDockerDaemon


def _image(
    image_id: str,
    source_hash: str,
    repository: str = "worker",
    parent_id: str | None = None,
) -> Image:
    return Image(
        image_id=image_id,
        source_hash=source_hash,
        references=(f"{repository}@sha256:{image_id}",),
        parent_id=parent_id,
    )


def _record(source_hash: str, image_id: str) -> dict:
    return {
        "source_hash": source_hash,
        "images": {
            name: {"reference": f"{name}@sha256:{image_id}"}
            for name in (
                "worker-base-common",
                "worker-base-claude",
                "worker-base-factory",
                "worker-base-codex",
            )
        },
    }


def test_dry_run_removes_only_generations_older_than_current_and_previous():
    images = [
        _image("current", "current", "worker-base-common"),
        _image("current-derived", "current"),
        _image("previous", "previous", "worker-base-common"),
        _image("previous-derived", "previous"),
        _image("stale", "stale", "worker-base-common"),
        _image("stale-derived", "stale"),
    ]

    plan = plan_cleanup(
        current_record=_record("current", "current"),
        previous_record=_record("previous", "previous"),
        images=images,
        running_image_ids=set(),
    )

    assert [item.image_id for item in plan.remove] == ["stale", "stale-derived"]
    assert render_plan(plan).splitlines() == [
        "KEEP current reason=current_generation source_hash=current",
        "KEEP current-derived reason=current_generation source_hash=current",
        "KEEP previous reason=previous_generation source_hash=previous",
        "KEEP previous-derived reason=previous_generation source_hash=previous",
        "REMOVE stale reason=stale_generation source_hash=stale",
        "REMOVE stale-derived reason=stale_generation source_hash=stale",
    ]


def test_record_named_image_is_kept_even_when_its_generation_is_stale():
    images = [
        _image("current", "current", "worker-base-common"),
        _image("previous", "previous", "worker-base-common"),
        _image("recorded", "older", "worker-base-claude"),
    ]
    record = _record("current", "current")
    record["images"]["worker-base-claude"] = {"reference": "worker-base-claude@sha256:recorded"}

    plan = plan_cleanup(
        current_record=record,
        previous_record=_record("previous", "previous"),
        images=images,
        running_image_ids=set(),
    )

    assert not plan.remove
    assert "KEEP recorded reason=deployed_record source_hash=older" in render_plan(plan)


def test_missing_record_disables_worker_image_removal():
    plan = plan_cleanup(
        current_record=None,
        previous_record=_record("previous", "previous"),
        images=[_image("stale", "stale", "worker-base-common")],
        running_image_ids=set(),
    )

    assert not plan.remove
    assert (
        render_plan(plan)
        == "KEEP worker-images reason=current_release_record_missing_or_unreadable"
    )


def test_running_container_image_is_kept_even_when_its_generation_is_stale():
    images = [
        _image("current", "current", "worker-base-common"),
        _image("previous", "previous", "worker-base-common"),
        _image("running", "stale"),
    ]

    plan = plan_cleanup(
        current_record=_record("current", "current"),
        previous_record=_record("previous", "previous"),
        images=images,
        running_image_ids={"running"},
    )

    assert not plan.remove
    assert "KEEP running reason=running_container source_hash=stale" in render_plan(plan)


def test_dry_run_prints_the_plan_without_removing_images(tmp_path, capsys):
    current = tmp_path / "current.json"
    previous = tmp_path / "previous.json"
    current.write_text(json.dumps(_record("current", "current")))
    previous.write_text(json.dumps(_record("previous", "previous")))
    calls: list[list[str]] = []
    inspected = {
        "current": {
            "Id": "current",
            "RepoTags": ["worker-base-common:latest"],
            "RepoDigests": ["worker-base-common@sha256:current"],
            "Parent": "",
            "Config": {"Labels": {"org.codegen.worker_source_hash": "current"}},
        },
        "previous": {
            "Id": "previous",
            "RepoTags": ["worker-base-common:previous"],
            "RepoDigests": ["worker-base-common@sha256:previous"],
            "Parent": "",
            "Config": {"Labels": {"org.codegen.worker_source_hash": "previous"}},
        },
        "stale": {
            "Id": "stale",
            "RepoTags": ["worker:stale"],
            "RepoDigests": [],
            "Parent": "previous",
            "Config": {"Labels": {"org.codegen.worker_source_hash": "stale"}},
        },
    }

    def docker(command: list[str]) -> str:
        calls.append(command)
        if command == ["image", "ls", "-q", "--no-trunc"]:
            return "current\nprevious\nstale\n"
        if command[:2] == ["image", "inspect"]:
            return json.dumps([inspected[command[2]]])
        if command == ["ps", "-a", "-q", "--no-trunc"]:
            return "stopped-container\n"
        if command == ["container", "inspect", "--format", "{{.Image}}", "stopped-container"]:
            return "previous\n"
        raise AssertionError(command)

    cleanup_worker_images(
        release_record=current,
        previous_release_record=previous,
        dry_run=True,
        run_docker=docker,
    )

    assert capsys.readouterr().out.splitlines() == [
        "KEEP current reason=current_generation source_hash=current",
        "KEEP previous reason=previous_generation source_hash=previous",
        "REMOVE stale reason=stale_generation source_hash=stale",
    ]
    assert all(command[:3] != ["image", "rm", "stale"] for command in calls)


def test_stopped_container_image_is_kept_by_real_docker_identity_query(tmp_path, capsys):
    current = tmp_path / "current.json"
    previous = tmp_path / "previous.json"
    current.write_text(json.dumps(_record("current", "current")))
    previous.write_text(json.dumps(_record("previous", "previous")))
    calls: list[list[str]] = []
    inspected = {
        image_id: {
            "Id": image_id,
            "RepoTags": [f"worker-base-common:{image_id}"],
            "RepoDigests": [f"worker-base-common@sha256:{image_id}"],
            "Parent": "",
            "Config": {"Labels": {"org.codegen.worker_source_hash": source_hash}},
        }
        for image_id, source_hash in (
            ("current", "current"),
            ("previous", "previous"),
            ("stale", "stale"),
        )
    }

    def docker(command: list[str]) -> str:
        calls.append(command)
        if command == ["image", "ls", "-q", "--no-trunc"]:
            return "current\nprevious\nstale\n"
        if command[:2] == ["image", "inspect"]:
            return json.dumps([inspected[command[2]]])
        if command == ["ps", "-a", "-q", "--no-trunc"]:
            return "stopped-container\n"
        if command == ["container", "inspect", "--format", "{{.Image}}", "stopped-container"]:
            return "stale\n"
        raise AssertionError(command)

    cleanup_worker_images(
        release_record=current,
        previous_release_record=previous,
        dry_run=True,
        run_docker=docker,
    )

    assert "KEEP stale reason=running_container source_hash=stale" in capsys.readouterr().out
    assert ["ps", "-a", "-q", "--no-trunc"] in calls


def test_live_cleanup_keeps_docker_refusal_and_continues_with_other_images(tmp_path, capsys):
    current = tmp_path / "current.json"
    previous = tmp_path / "previous.json"
    current.write_text(json.dumps(_record("current", "current")))
    previous.write_text(json.dumps(_record("previous", "previous")))
    removals: list[str] = []
    inspected = {
        image_id: {
            "Id": image_id,
            "RepoTags": [f"worker-base-common:{image_id}"],
            "RepoDigests": [f"worker-base-common@sha256:{image_id}"],
            "Parent": "",
            "Config": {"Labels": {"org.codegen.worker_source_hash": source_hash}},
        }
        for image_id, source_hash in (
            ("current", "current"),
            ("previous", "previous"),
            ("refused", "stale"),
            ("removed", "stale"),
        )
    }

    def docker(command: list[str]) -> str:
        if command == ["image", "ls", "-q", "--no-trunc"]:
            return "current\nprevious\nrefused\nremoved\n"
        if command[:2] == ["image", "inspect"]:
            return json.dumps([inspected[command[2]]])
        if command == ["ps", "-a", "-q", "--no-trunc"]:
            return ""
        if command[:2] == ["image", "rm"]:
            removals.append(command[2])
            if command[2] == "refused":
                raise subprocess.CalledProcessError(
                    1, command, stderr="conflict: unable to delete refused (must be forced)"
                )
            return ""
        raise AssertionError(command)

    cleanup_worker_images(
        release_record=current,
        previous_release_record=previous,
        dry_run=False,
        run_docker=docker,
    )

    assert removals == ["refused", "removed"]
    assert "KEEP refused reason=docker_refused source_hash=stale" in capsys.readouterr().out


def test_live_cleanup_removes_derived_images_before_their_base(tmp_path):
    current = tmp_path / "current.json"
    previous = tmp_path / "previous.json"
    current.write_text(json.dumps(_record("current", "current")))
    previous.write_text(json.dumps(_record("previous", "previous")))
    removals: list[str] = []

    def docker(command: list[str]) -> str:
        if command == ["image", "ls", "-q", "--no-trunc"]:
            return "current\nprevious\nstale-base\nstale-derived\n"
        source_hash = "stale" if command[2].startswith("stale") else command[2]
        if command[:2] == ["image", "inspect"]:
            return json.dumps(
                [
                    {
                        "Id": command[2],
                        "RepoTags": [f"worker-base-common:{command[2]}"],
                        "RepoDigests": [f"worker-base-common@sha256:{command[2]}"],
                        "Parent": "stale-base" if command[2] == "stale-derived" else "",
                        "Config": {"Labels": {"org.codegen.worker_source_hash": source_hash}},
                    }
                ]
            )
        if command == ["ps", "-a", "-q", "--no-trunc"]:
            return ""
        if command[:2] == ["image", "rm"]:
            removals.append(command[2])
            return ""
        raise AssertionError(command)

    cleanup_worker_images(
        release_record=current,
        previous_release_record=previous,
        dry_run=False,
        run_docker=docker,
    )

    assert removals == ["stale-derived", "stale-base"]


# --- removal against the daemon's rules ---------------------------------------------------

REGISTRY = "ghcr.io/vladmesh/codegen-orchestrator"


def _records(tmp_path) -> tuple:
    current = tmp_path / "current.json"
    previous = tmp_path / "previous.json"
    current.write_text(json.dumps(_record("current", "current")))
    previous.write_text(json.dumps(_record("previous", "previous")))
    return current, previous


def _daemon() -> FakeDockerDaemon:
    """A host with the current and previous generation and one stale, retagged generation."""
    daemon = FakeDockerDaemon()
    for image_id in ("current", "previous"):
        daemon.add(
            image_id,
            f"{REGISTRY}/worker-base-common@sha256:{image_id}",
            labels={"org.codegen.worker_source_hash": image_id},
        )
    # Pulled by digest, then named locally: two repositories, so docker refuses its ID.
    daemon.add(
        "stale",
        "worker-base-common:stale",
        f"{REGISTRY}/worker-base-common@sha256:stale",
        labels={"org.codegen.worker_source_hash": "stale"},
    )
    return daemon


def _stale(daemon: FakeDockerDaemon, image_id: str, *names: str) -> None:
    daemon.add(image_id, *names, labels={"org.codegen.worker_source_hash": "stale"})


def _cleanup(tmp_path, daemon: FakeDockerDaemon) -> list[str]:
    current, previous = _records(tmp_path)
    return cleanup_worker_images(
        release_record=current, previous_release_record=previous, dry_run=False, run_docker=daemon
    )


def test_live_cleanup_removes_by_id_and_untags_only_what_docker_refuses_by_id(tmp_path, capsys):
    daemon = _daemon()
    _stale(daemon, "one-name", "worker:stale")

    assert _cleanup(tmp_path, daemon) == []

    assert set(daemon.images) == {"current", "previous"}
    assert daemon.removals == [
        "one-name",
        "stale",
        "worker-base-common:stale",
        f"{REGISTRY}/worker-base-common@sha256:stale",
    ]
    out = capsys.readouterr().out.splitlines()
    assert "REMOVED one-name source_hash=stale" in out
    assert "REMOVED stale source_hash=stale" in out


def test_a_digest_whose_tag_took_it_along_is_already_gone_not_an_error(tmp_path, capsys):
    daemon = _daemon()
    _stale(
        daemon,
        "retagged",
        "worker-base-claude:stale",
        "worker-base-claude@sha256:retagged",
        f"{REGISTRY}/worker-base-claude@sha256:retagged",
    )

    assert _cleanup(tmp_path, daemon) == []

    assert "retagged" not in daemon.images
    out = capsys.readouterr().out.splitlines()
    assert "GONE retagged reference=worker-base-claude@sha256:retagged reason=already_gone" in out
    assert "REMOVED retagged source_hash=stale" in out


def test_images_and_tags_already_gone_are_skipped_and_the_rest_removed(tmp_path, capsys):
    daemon = _daemon()
    daemon.listed_but_gone = ["vanished-before-inspect"]
    _stale(daemon, "vanishes-before-rm", "worker:vanishes")
    _stale(daemon, "loses-a-tag", "worker:stale-a", "worker:stale-b")

    def gone(host: FakeDockerDaemon) -> None:
        del host.images["vanishes-before-rm"]
        host.untag("worker:stale-a")

    daemon.before_first_removal = gone

    assert _cleanup(tmp_path, daemon) == []

    assert set(daemon.images) == {"current", "previous"}
    out = capsys.readouterr().out.splitlines()
    assert "GONE vanished-before-inspect reason=already_gone" in out
    assert "GONE vanishes-before-rm reason=already_gone source_hash=stale" in out
    assert "REMOVED loses-a-tag source_hash=stale" in out
    assert "REMOVED stale source_hash=stale" in out


def test_an_image_in_use_or_failing_is_reported_and_the_rest_still_removed(tmp_path, capsys):
    daemon = _daemon()
    _stale(daemon, "a-in-use", "worker:in-use")
    _stale(daemon, "b-broken", "worker:broken")
    daemon.failures["b-broken"] = "Error response from daemon: driver failed\n"
    # A container started from it after the inventory was read.
    daemon.before_first_removal = lambda host: host.containers.update(late="a-in-use")

    assert _cleanup(tmp_path, daemon) == ["b-broken"]

    assert set(daemon.images) == {"current", "previous", "a-in-use", "b-broken"}
    out = capsys.readouterr().out.splitlines()
    assert "KEEP a-in-use reason=docker_refused source_hash=stale" in out
    assert "FAIL b-broken error=Error response from daemon: driver failed source_hash=stale" in out
    assert "REMOVED stale source_hash=stale" in out


def test_the_command_exits_non_zero_only_for_a_failed_removal(tmp_path, monkeypatch):
    current, previous = _records(tmp_path)
    argv = ["--release-record", str(current), "--previous-release-record", str(previous)]
    # Whatever the command resolves, it never reaches this host's docker.
    monkeypatch.setattr("shutil.which", lambda _name: None)
    daemon = _daemon()
    daemon.listed_but_gone = ["gone"]
    monkeypatch.setattr("scripts.cleanup_worker_images._run_docker", daemon)
    assert main(argv) == 0

    daemon = _daemon()
    daemon.failures["stale"] = "Error response from daemon: driver failed\n"
    monkeypatch.setattr("scripts.cleanup_worker_images._run_docker", daemon)
    assert main(argv) == 1
