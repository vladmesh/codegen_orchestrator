"""The deployed service release on the host: its records, its compose override, its cleanup.

`scripts/service_release.py` is what makes compose run the pulled release and what keeps
the previous release for a rollback. The compose override is checked against the real
compose files of both deploy contours, so a service that builds locally and is missing
from the release fails here rather than being built on a production host.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
import subprocess

import pytest
import yaml

from scripts.service_release import (
    Image,
    Release,
    ServiceReleaseError,
    cleanup_service_images,
    compose_override,
    load_release,
    main,
    parse_release,
    plan_cleanup,
    readback,
    rotate_previous_record,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
REAL_RECORD = json.loads(
    (REPO_ROOT / "tests" / "unit" / "fixtures" / "service-release-7f93d8b7.json").read_text()
)
CURRENT_SHA = REAL_RECORD["git_sha"]
PREVIOUS_SHA = "0123456789abcdef0123456789abcdef01234567"
STALE_SHA = "fedcba9876543210fedcba9876543210fedcba98"
REGISTRY = "ghcr.io/vladmesh/codegen-orchestrator"


def _record(git_sha: str, digest_seed: str) -> dict:
    record = copy.deepcopy(REAL_RECORD)
    record["git_sha"] = git_sha
    for name, entry in record["images"].items():
        digest = f"sha256:{digest_seed}-{name}"
        entry.update(digest=digest, reference=f"{entry['repository']}@{digest}")
    return record


# --- the record ----------------------------------------------------------------------


def test_the_real_published_record_is_a_release():
    release = parse_release(REAL_RECORD)

    assert release is not None
    assert release.git_sha == CURRENT_SHA
    assert release.references["api"] == REAL_RECORD["images"]["api"]["reference"]
    assert len(release.references) == 10


@pytest.mark.parametrize(
    "corrupt",
    [
        lambda r: r.pop("schema_version"),
        lambda r: r.update(schema_version=2),
        lambda r: r.update(git_sha="7f93d8b7"),
        lambda r: r.update(source_hash=""),
        lambda r: r.update(images={}),
        lambda r: r["images"]["api"].update(reference=f"{REGISTRY}/api:latest"),
        lambda r: r["images"]["api"].update(reference=f"{REGISTRY}/langgraph@sha256:x"),
    ],
)
def test_anything_but_a_release_record_is_not_a_release(corrupt):
    record = copy.deepcopy(REAL_RECORD)
    corrupt(record)

    assert parse_release(record) is None


def test_rotation_refuses_a_revision_that_is_not_a_full_sha(tmp_path):
    with pytest.raises(ServiceReleaseError):
        rotate_previous_record(tmp_path / "current", tmp_path / "previous", "HEAD")


def test_a_first_deploy_starts_with_an_empty_previous_record(tmp_path):
    rotate_previous_record(tmp_path / "missing.json", tmp_path / "previous.json", CURRENT_SHA)

    assert (tmp_path / "previous.json").read_text() == ""
    assert load_release(tmp_path / "previous.json") is None


# --- the compose override, against the real compose files -----------------------------


class _ComposeLoader(yaml.SafeLoader):
    """SafeLoader that reads compose's own merge tags (`!reset`, `!override`) as values."""


_ComposeLoader.add_multi_constructor("!", lambda loader, suffix, node: None)


def _resolved(*files: str) -> dict:
    """The part of `docker compose config` the override reads: each service's image and build."""
    services: dict[str, dict] = {}
    for name in files:
        document = yaml.load((REPO_ROOT / name).read_text(), _ComposeLoader)  # noqa: S506
        for service_name, service in (document.get("services") or {}).items():
            merged = services.setdefault(service_name, {})
            for key in ("image", "build"):
                if key in (service or {}):
                    merged[key] = service[key]
    return {"services": services}


CONTOURS = {
    "production": ("docker-compose.yml", "docker-compose.prod.yml"),
    "stand": ("docker-compose.yml", "docker-compose.prod.yml", "docker-compose.stand.yml"),
}


@pytest.mark.parametrize("contour", sorted(CONTOURS))
def test_every_locally_built_service_runs_its_released_digest(contour):
    config = _resolved(*CONTOURS[contour])
    release = parse_release(REAL_RECORD)

    override = yaml.safe_load(compose_override(config, release))["services"]

    built = {name for name, service in config["services"].items() if "build" in service}
    assert set(override) == built, "every service compose would build is overridden, no other"
    for name in ("langgraph", "architect", "engineering-worker", "deploy-worker", "qa-worker"):
        assert override[name]["image"] == release.references["langgraph"]
    for name in ("scheduler-pipeline", "scheduler-infrastructure", "scheduler-maintenance"):
        assert override[name]["image"] == release.references["scheduler"]
    for name in ("admin-frontend", "user-dashboard", "api", "worker-manager"):
        assert override[name]["image"] == release.references[name]
    assert {service["image"] for service in override.values()} == set(
        release.references.values()
    ), "every released image runs somewhere"
    assert all("@sha256:" in service["image"] for service in override.values())


def test_the_development_compose_file_still_builds_locally():
    """Nothing about the release reaches a plain `docker compose` without the override."""
    config = _resolved("docker-compose.yml")

    for name, service in config["services"].items():
        if "build" in service:
            assert service["image"].startswith("codegen-orchestrator/"), name
            assert service["image"].endswith(":local"), name


@pytest.mark.parametrize(
    ("service", "message"),
    [
        ({"build": {"context": "."}}, "not as codegen-orchestrator/<image>:local"),
        ({"build": {}, "image": "someone/api:local"}, "not as codegen-orchestrator"),
        ({"build": {}, "image": "codegen-orchestrator/api:dev"}, "not as codegen-orchestrator"),
        ({"build": {}, "image": "codegen-orchestrator/brand-new:local"}, "does not contain"),
    ],
)
def test_a_build_the_release_does_not_cover_fails_instead_of_building(service, message):
    config = {"services": {"new": service, "redis": {"image": "redis:7"}}}

    with pytest.raises(ServiceReleaseError, match=message):
        compose_override(config, parse_release(REAL_RECORD))


def test_a_configuration_that_builds_nothing_is_refused():
    with pytest.raises(ServiceReleaseError):
        compose_override({"services": {"redis": {"image": "redis:7"}}}, parse_release(REAL_RECORD))


def test_the_override_command_writes_the_file_whole(tmp_path):
    config = tmp_path / "config.json"
    config.write_text(json.dumps(_resolved(*CONTOURS["production"])))
    record = tmp_path / "record.json"
    record.write_text(json.dumps(REAL_RECORD))
    output = tmp_path / "deployed-service-images.compose.yml"
    output.write_text("stale\n")

    status = main(
        [
            "compose-override",
            "--compose-config",
            str(config),
            "--record",
            str(record),
            "--output",
            str(output),
        ]
    )

    assert status == 0
    services = yaml.safe_load(output.read_text())["services"]
    assert services["api"]["image"] == REAL_RECORD["images"]["api"]["reference"]
    assert not list(tmp_path.glob("*.next"))


def test_the_override_command_refuses_an_unusable_record_and_keeps_the_old_file(tmp_path):
    config = tmp_path / "config.json"
    config.write_text(json.dumps(_resolved(*CONTOURS["production"])))
    record = tmp_path / "record.json"
    record.write_text("")
    output = tmp_path / "override.yml"
    output.write_text("previous override\n")

    status = main(
        [
            "compose-override",
            "--compose-config",
            str(config),
            "--record",
            str(record),
            "--output",
            str(output),
        ]
    )

    assert status == 1
    assert output.read_text() == "previous override\n"


# --- cleanup ---------------------------------------------------------------------------


def _release(git_sha: str, seed: str) -> Release:
    release = parse_release(_record(git_sha, seed))
    assert release is not None
    return release


def _pulled(release: Release, name: str) -> Image:
    return Image(
        image_id=f"id-{release.git_sha[:6]}-{name}",
        references=(
            f"codegen-orchestrator/{name}:{release.git_sha}",
            release.references[name],
        ),
    )


def test_cleanup_keeps_current_and_previous_and_removes_older_releases_and_host_builds():
    current, previous, stale = (
        _release(CURRENT_SHA, "current"),
        _release(PREVIOUS_SHA, "previous"),
        _release(STALE_SHA, "stale"),
    )
    images = [
        _pulled(current, "api"),
        _pulled(previous, "api"),
        _pulled(stale, "api"),
        Image("id-digest-only", (stale.references["langgraph"],)),
        Image("id-host-build", ("codegen-orchestrator/langgraph:local",)),
        Image("id-host-build-in-use", ("codegen-orchestrator/api:local",)),
        Image("id-redis", ("redis:7.4.10-alpine",)),
        Image("id-worker", ("worker-base-common:latest",)),
    ]

    plan = plan_cleanup(
        current=current,
        previous=previous,
        images=images,
        container_image_ids={"id-host-build-in-use"},
    )

    assert {item.image_id for item in plan.keep} == {
        f"id-{CURRENT_SHA[:6]}-api",
        f"id-{PREVIOUS_SHA[:6]}-api",
        "id-host-build-in-use",
    }
    assert {item.image_id for item in plan.remove} == {
        f"id-{STALE_SHA[:6]}-api",
        "id-digest-only",
        "id-host-build",
    }


@pytest.mark.parametrize("missing", ["current", "previous"])
def test_cleanup_without_both_records_removes_nothing(missing):
    releases = {"current": _release(CURRENT_SHA, "c"), "previous": _release(PREVIOUS_SHA, "p")}
    releases[missing] = None

    plan = plan_cleanup(
        **releases,
        images=[Image("id-host-build", ("codegen-orchestrator/api:local",))],
        container_image_ids=set(),
    )

    assert plan.remove == ()
    assert plan.disabled_reason


class FakeDocker:
    def __init__(self, images: dict[str, list[str]], refuse: set[str] = frozenset()) -> None:
        self.images = images
        self.refuse = refuse
        self.removed: list[list[str]] = []

    def __call__(self, command: list[str]) -> str:
        if command[:2] == ["image", "ls"]:
            return "\n".join(self.images) + "\n"
        if command[:2] == ["image", "inspect"]:
            references = self.images[command[2]]
            return json.dumps(
                [
                    {
                        "Id": command[2],
                        "RepoTags": [ref for ref in references if "@" not in ref],
                        "RepoDigests": [ref for ref in references if "@" in ref],
                    }
                ]
            )
        if command[:2] == ["ps", "-a"]:
            return ""
        if command[:2] == ["image", "rm"]:
            if set(command[2:]) & self.refuse:
                raise subprocess.CalledProcessError(
                    1, command, stderr="Error response from daemon: conflict: image is being used"
                )
            self.removed.append(command[2:])
            return ""
        raise AssertionError(f"unexpected docker call {command}")


def _write_records(tmp_path: Path) -> tuple[Path, Path]:
    current = tmp_path / "deployed-service-images.json"
    previous = tmp_path / "previous-deployed-service-images.json"
    current.write_text(json.dumps(_record(CURRENT_SHA, "c")))
    previous.write_text(json.dumps(_record(PREVIOUS_SHA, "p")))
    return current, previous


def test_cleanup_removes_a_stale_image_by_every_name_it_has(tmp_path, capsys):
    current, previous = _write_records(tmp_path)
    stale = _record(STALE_SHA, "s")["images"]["api"]["reference"]
    docker = FakeDocker(
        {
            "id-stale": [f"codegen-orchestrator/api:{STALE_SHA}", stale],
            "id-current": [_record(CURRENT_SHA, "c")["images"]["api"]["reference"]],
        }
    )

    cleanup_service_images(
        current_record=current, previous_record=previous, dry_run=False, run_docker=docker
    )

    assert docker.removed == [[f"codegen-orchestrator/api:{STALE_SHA}", stale]]
    assert "KEEP id-current reason=deployed_release" in capsys.readouterr().out


def test_cleanup_dry_run_and_docker_refusal_remove_nothing(tmp_path, capsys):
    current, previous = _write_records(tmp_path)
    docker = FakeDocker(
        {"id-stale": ["codegen-orchestrator/api:local"]}, refuse={"codegen-orchestrator/api:local"}
    )

    cleanup_service_images(
        current_record=current, previous_record=previous, dry_run=True, run_docker=docker
    )
    assert docker.removed == []

    cleanup_service_images(
        current_record=current, previous_record=previous, dry_run=False, run_docker=docker
    )
    assert docker.removed == []
    assert "KEEP id-stale reason=docker_refused" in capsys.readouterr().out


# --- readback ------------------------------------------------------------------------------

DEPLOY_PATH = Path("/opt/codegen_orchestrator")


def _live_config(release: Release) -> dict:
    """The live stack as `docker compose config` renders it with the override applied."""
    return {
        "name": "codegen_orchestrator",
        "services": {
            "api": {
                "build": {"context": str(DEPLOY_PATH)},
                "image": release.references["api"],
                "volumes": [
                    {
                        "type": "bind",
                        "source": "/opt/secrets/github_app.pem",
                        "target": "/app/keys/github_app.pem",
                        "read_only": True,
                    }
                ],
            },
            "telegram_bot": {
                "build": {"context": str(DEPLOY_PATH)},
                "image": release.references["telegram_bot"],
                "scale": 0,
            },
            "caddy": {
                "image": "caddy:2.11.4-alpine",
                "volumes": [
                    {
                        "type": "bind",
                        "source": f"{DEPLOY_PATH}/infra/Caddyfile",
                        "target": "/etc/caddy/Caddyfile",
                    }
                ],
            },
        },
    }


PROJECT = "codegen_orchestrator"


def _container(service: str, image: str, mounts: list[dict], labels: dict | None = None) -> dict:
    return {
        "Image": image,
        "State": {"Running": True},
        "Config": {
            "Labels": {
                "com.docker.compose.project": PROJECT,
                "com.docker.compose.service": service,
                **(labels or {}),
            }
        },
        "Mounts": mounts,
    }


class ReadbackDocker:
    """docker ps / inspect, faked: the project's api and caddy containers, and whatever a test
    changes or adds."""

    def __init__(self, release: Release, source_hash: str) -> None:
        self.image_ids = {
            reference: f"sha256:id-{name}" for name, reference in release.references.items()
        }
        self.api = _container(
            "api",
            "sha256:id-api",
            [
                {
                    "Type": "bind",
                    "Source": "/opt/secrets/github_app.pem",
                    "Destination": "/app/keys/github_app.pem",
                }
            ],
            {"org.codegen.worker_source_hash": source_hash},
        )
        self.caddy = _container(
            "caddy",
            "sha256:id-caddy",
            [
                {
                    "Type": "bind",
                    "Source": f"{DEPLOY_PATH}/infra/Caddyfile",
                    "Destination": "/etc/caddy/Caddyfile",
                },
                {"Type": "volume", "Source": "/var/lib/docker/volumes/caddy_data/_data"},
            ],
        )
        self.containers = {"c0ffee00api1": self.api, "c0ffee0caddy": self.caddy}
        self.calls: list[list[str]] = []

    def __call__(self, command: list[str]) -> str:
        self.calls.append(command)
        if command[:2] == ["image", "inspect"]:
            return self.image_ids[command[-1]] + "\n"
        if command[:1] == ["ps"]:
            filters = {
                key: value
                for key, _, value in (
                    command[index + 1].removeprefix("label=").partition("=")
                    for index, item in enumerate(command)
                    if item == "--filter"
                )
            }
            assert filters["com.docker.compose.project"] == PROJECT
            return "".join(
                f"{container_id}\n"
                for container_id, container in self.containers.items()
                if ("-a" in command or container["State"]["Running"])
                and all(
                    container["Config"]["Labels"].get(key) == value
                    for key, value in filters.items()
                )
            )
        if command[:2] == ["container", "inspect"]:
            return json.dumps([self.containers[command[-1]]])
        raise AssertionError(f"unexpected docker call {command}")


def _readback(tmp_path: Path, docker: ReadbackDocker, config: dict | None = None) -> list[str]:
    record = tmp_path / "deployed-service-images.json"
    record.write_text(json.dumps(REAL_RECORD))
    release = parse_release(REAL_RECORD)

    return readback(
        compose_config=config or _live_config(release),
        record=record,
        deploy_path=DEPLOY_PATH,
        run_docker=docker,
    )


def test_readback_confirms_a_container_running_the_recorded_digest(tmp_path, capsys):
    docker = ReadbackDocker(parse_release(REAL_RECORD), REAL_RECORD["source_hash"])

    assert _readback(tmp_path, docker) == []
    out = capsys.readouterr().out
    assert f"OK api c0ffee00api1 image={REAL_RECORD['images']['api']['reference']}" in out
    assert "SKIP telegram_bot scale=0" in out
    assert f"OK 2 containers of project {PROJECT} mount no checkout source" in out
    assert [
        "ps",
        "-a",
        "-q",
        "--no-trunc",
        "--filter",
        f"label=com.docker.compose.project={PROJECT}",
    ] in docker.calls
    assert all(call[:1] in (["ps"], ["image"], ["container"]) for call in docker.calls)
    assert not [call for call in docker.calls if call[:2] in (["image", "rm"], ["compose", "up"])]


def test_readback_reads_the_configuration_with_or_without_the_override(tmp_path):
    release = parse_release(REAL_RECORD)
    config = _live_config(release)
    config["services"]["api"]["image"] = "codegen-orchestrator/api:local"
    docker = ReadbackDocker(release, REAL_RECORD["source_hash"])

    assert _readback(tmp_path, docker, config) == []


def test_readback_fails_a_container_on_another_image(tmp_path):
    docker = ReadbackDocker(parse_release(REAL_RECORD), REAL_RECORD["source_hash"])
    docker.api["Image"] = "sha256:built-on-the-host"

    (problem,) = _readback(tmp_path, docker)

    assert problem.startswith("api c0ffee00api1: runs image sha256:built-on-the-host")


@pytest.mark.parametrize("stamped", [None, "", "0123456789abcdef"])
def test_readback_fails_an_empty_or_foreign_source_hash(tmp_path, stamped):
    docker = ReadbackDocker(parse_release(REAL_RECORD), REAL_RECORD["source_hash"])
    labels = docker.api["Config"]["Labels"]
    labels.pop("org.codegen.worker_source_hash")
    if stamped is not None:
        labels["org.codegen.worker_source_hash"] = stamped

    (problem,) = _readback(tmp_path, docker)

    assert f"carries org.codegen.worker_source_hash={stamped!r}" in problem


def test_readback_fails_a_source_mount_in_the_config_and_in_the_container(tmp_path):
    release = parse_release(REAL_RECORD)
    config = _live_config(release)
    config["services"]["api"]["volumes"].append(
        {"type": "bind", "source": f"{DEPLOY_PATH}/shared", "target": "/app/shared"}
    )
    docker = ReadbackDocker(release, REAL_RECORD["source_hash"])
    docker.api["Mounts"].append(
        {"Type": "bind", "Source": f"{DEPLOY_PATH}/services/api/src", "Destination": "/app/src"}
    )

    problems = _readback(tmp_path, docker, config)

    assert problems == [
        "config: service api bind-mounts checkout source shared at /app/shared",
        "api c0ffee00api1: bind-mounts checkout source services/api/src at /app/src",
    ]


def test_readback_fails_a_non_build_container_with_a_drifted_source_mount(tmp_path, capsys):
    """caddy is not a build service; its live container still may not read checkout source,
    even when the configuration it would be created from today is clean."""
    docker = ReadbackDocker(parse_release(REAL_RECORD), REAL_RECORD["source_hash"])
    docker.caddy["Mounts"].append(
        {"Type": "bind", "Source": f"{DEPLOY_PATH}/shared", "Destination": "/srv/shared"}
    )

    problems = _readback(tmp_path, docker)

    assert problems == ["caddy c0ffee0caddy: bind-mounts checkout source shared at /srv/shared"]
    assert "mount no checkout source" not in capsys.readouterr().out


@pytest.mark.parametrize("running", [True, False], ids=["running", "created"])
def test_readback_fails_an_orphan_container_with_a_source_mount(tmp_path, running):
    """A container of a service the configuration no longer names keeps the mounts it was
    created with; running or only created, it is still the project's."""
    docker = ReadbackDocker(parse_release(REAL_RECORD), REAL_RECORD["source_hash"])
    orphan = _container(
        "old-scheduler",
        "sha256:built-long-ago",
        [
            {
                "Type": "bind",
                "Source": f"{DEPLOY_PATH}/services/scheduler/src",
                "Destination": "/app/src",
            }
        ],
    )
    orphan["State"]["Running"] = running
    docker.containers["0dd0rphan001"] = orphan

    problems = _readback(tmp_path, docker)

    assert problems == [
        "old-scheduler 0dd0rphan001 (orphan): bind-mounts checkout source "
        "services/scheduler/src at /app/src"
    ]


def test_readback_passes_a_clean_project_with_an_orphan_that_mounts_no_source(tmp_path, capsys):
    docker = ReadbackDocker(parse_release(REAL_RECORD), REAL_RECORD["source_hash"])
    docker.containers["0dd0rphan001"] = _container(
        "old-scheduler",
        "sha256:built-long-ago",
        [{"Type": "bind", "Source": "/data/workspaces", "Destination": "/data/workspaces"}],
    )

    assert _readback(tmp_path, docker) == []
    assert f"OK 3 containers of project {PROJECT} mount no checkout source" in (
        capsys.readouterr().out
    )


def test_readback_fails_a_build_service_with_no_running_container(tmp_path):
    release = parse_release(REAL_RECORD)
    config = _live_config(release)
    del config["services"]["telegram_bot"]["scale"]

    problems = _readback(tmp_path, ReadbackDocker(release, REAL_RECORD["source_hash"]), config)

    assert problems == ["telegram_bot: no running container"]


def test_readback_command_exits_non_zero_on_a_problem(tmp_path, monkeypatch, capsys):
    release = parse_release(REAL_RECORD)
    docker = ReadbackDocker(release, REAL_RECORD["source_hash"])
    docker.api["Image"] = "sha256:other"
    monkeypatch.setattr("scripts.service_release._run_docker", docker)
    record = tmp_path / "deployed-service-images.json"
    record.write_text(json.dumps(REAL_RECORD))
    config = tmp_path / "compose.json"
    config.write_text(json.dumps(_live_config(release)))

    status = main(
        [
            "readback",
            "--compose-config",
            str(config),
            "--record",
            str(record),
            "--deploy-path",
            str(DEPLOY_PATH),
        ]
    )

    assert status == 1
    assert "FAIL api c0ffee00api1: runs image sha256:other" in capsys.readouterr().out
