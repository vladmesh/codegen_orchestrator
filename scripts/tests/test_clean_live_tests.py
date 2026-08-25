import ast
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
from types import SimpleNamespace

import httpx
import pytest

from scripts import clean_live_tests

_LIVE_TESTS = Path(__file__).resolve().parents[2] / "tests" / "live"


def _result(stdout="0\n", returncode=0, stderr=""):
    return SimpleNamespace(stdout=stdout, returncode=returncode, stderr=stderr)


def _live_harness_modules():
    """The harness modules recovery drives, imported the way recovery imports them."""
    if str(_LIVE_TESTS) not in sys.path:
        sys.path.insert(0, str(_LIVE_TESTS))
    import pipeline_helpers
    import run_cleanup
    import run_evidence

    return pipeline_helpers, run_cleanup, run_evidence


@pytest.mark.parametrize(
    ("project_count", "allocation_count", "worker_keys", "manifest", "label"),
    [
        (1, 0, "", False, "projects=1"),
        (0, 1, "", False, "allocations=1"),
        (0, 0, "worker:meta:w1\n", False, "workers=1"),
        (0, 0, "", True, "ownership_manifests=1"),
    ],
)
def test_verify_no_residue_fails_closed(
    monkeypatch, tmp_path, project_count, allocation_count, worker_keys, manifest, label
):
    calls = iter(
        [_result(f"{project_count}\n"), _result(f"{allocation_count}\n"), _result(worker_keys)]
    )
    monkeypatch.setattr(clean_live_tests, "run_cmd", lambda *args, **kwargs: next(calls))
    monkeypatch.setattr(clean_live_tests, "collect_remote_residue", dict)
    monkeypatch.setattr(clean_live_tests, "ORCHESTRATOR_ROOT", str(tmp_path))
    if manifest:
        path = Path(tmp_path) / ".live-manifests" / "run.json"
        path.parent.mkdir()
        path.write_text("{}")

    with pytest.raises(clean_live_tests.CleanupFailure, match=label):
        clean_live_tests.verify_no_residue()


def test_verify_no_residue_accepts_proven_absence(monkeypatch, tmp_path):
    calls = iter([_result(), _result(), _result(stdout="")])
    monkeypatch.setattr(clean_live_tests, "run_cmd", lambda *args, **kwargs: next(calls))
    monkeypatch.setattr(clean_live_tests, "collect_remote_residue", dict)
    monkeypatch.setattr(clean_live_tests, "ORCHESTRATOR_ROOT", str(tmp_path))

    clean_live_tests.verify_no_residue()


def test_verify_no_residue_reports_a_stack_no_db_row_points_at(monkeypatch, tmp_path):
    """A live orphan stack must break the verdict, not be invisible to it.

    Every other counter here reads the database, the registry or Redis. The
    orphan that broke two mega runs had no rows left in any of them — cleanup
    had already deleted its project — while its containers held port 8000 on the
    target. A clean verdict from those counters alone is exactly the false
    "cleanup fully complete" this closes.
    """
    calls = iter([_result(), _result(), _result(stdout="")])
    monkeypatch.setattr(clean_live_tests, "run_cmd", lambda *args, **kwargs: next(calls))
    monkeypatch.setattr(clean_live_tests, "ORCHESTRATOR_ROOT", str(tmp_path))
    monkeypatch.setattr(
        clean_live_tests,
        "collect_remote_residue",
        lambda: {"vps-1": [f"container live-te-{'a' * 32}-backend-1"]},
    )

    with pytest.raises(clean_live_tests.CleanupFailure) as error:
        clean_live_tests.verify_no_residue()

    assert "deployed_stacks=1" in str(error.value)
    assert f"vps-1: container live-te-{'a' * 32}-backend-1" in str(error.value)


def test_allocation_residue_query_qualifies_project_title(monkeypatch, tmp_path):
    commands = []
    results = iter([_result(), _result(), _result(stdout="")])
    monkeypatch.setattr(
        clean_live_tests,
        "run_cmd",
        lambda cmd, **kwargs: (commands.append(cmd), next(results))[1],
    )
    monkeypatch.setattr(clean_live_tests, "ORCHESTRATOR_ROOT", str(tmp_path))

    clean_live_tests.collect_residue_state(remote_residue={})

    allocation_sql = commands[1][-1]
    assert "JOIN projects p ON p.id = r.project_id" in allocation_sql
    assert "p.title LIKE" in allocation_sql
    assert " WHERE title LIKE" not in allocation_sql
    assert "name LIKE" not in allocation_sql


def test_get_test_projects_reads_title_and_slug(monkeypatch):
    captured: dict[str, str] = {}

    def fake_run_cmd(cmd, **kwargs):
        captured["sql"] = cmd[cmd.index("-c") + 1]
        return _result("project-1|live-test-old|live-te-11111111111111111111111111111111\n")

    monkeypatch.setattr(clean_live_tests, "run_cmd", fake_run_cmd)

    projects = clean_live_tests.get_test_projects()

    assert projects == [
        {
            "id": "project-1",
            "title": "live-test-old",
            "slug": "live-te-11111111111111111111111111111111",
        }
    ]
    assert "SELECT id, title, slug FROM projects" in captured["sql"]
    assert "name" not in captured["sql"]


def test_get_test_projects_reads_every_row_psql_printed(monkeypatch):
    """More than one live-test project must not collapse into zero.

    psql separates rows with real newlines. Splitting the answer on the literal
    two-character string `\\n` left one unparsable line whose `|` count never
    equalled three, so this returned an empty list without saying so — and an
    empty list silently emptied both the remote sweep's target list and the
    project half of the residue verdict.
    """

    def fake_run_cmd(cmd, **kwargs):
        return _result(
            "project-1|live-test-a|live-te-" + "1" * 32 + "\n"
            "project-2|live-crud-b|live-cr-" + "2" * 32 + "\n"
        )

    monkeypatch.setattr(clean_live_tests, "run_cmd", fake_run_cmd)

    projects = clean_live_tests.get_test_projects()

    assert [project["id"] for project in projects] == ["project-1", "project-2"]
    assert [project["slug"] for project in projects] == [
        "live-te-" + "1" * 32,
        "live-cr-" + "2" * 32,
    ]


def test_local_docker_filter_is_a_regexp_alternation(monkeypatch):
    """`docker ps --filter name=` takes a regexp; `\\|` matched a literal pipe.

    Escaped, the filter asked for containers whose name contains `live-test|…`
    literally — nothing — so this step always reported no test containers.
    """
    commands: list[list[str]] = []

    def fake_run_cmd(cmd, **kwargs):
        commands.append(cmd)
        return _result(stdout="")

    monkeypatch.setattr(clean_live_tests, "run_cmd", fake_run_cmd)

    clean_live_tests.clean_local_docker()

    assert commands[0] == [
        "docker",
        "ps",
        "-aq",
        "--filter",
        "name=live-test|live-crud|mega-test",
    ]


def test_local_workspace_sweep_keeps_every_active_repository(monkeypatch):
    """Workspaces of live repositories must survive the orphan sweep.

    The repository ids arrive as psql rows, one per line. Split on the literal
    `\\n`, they became a single unmatched blob, every workspace looked orphaned
    and the sweep deleted the checkouts of running repositories.
    """
    commands: list[list[str]] = []

    def fake_run_cmd(cmd, **kwargs):
        commands.append(cmd)
        if "psql" in cmd:
            return _result(stdout="repo-1\nrepo-2\n")
        return _result(stdout="")

    monkeypatch.setattr(clean_live_tests, "run_cmd", fake_run_cmd)

    clean_live_tests.clean_local_workspaces()

    # The set's order is not stable across runs; its contents are the contract.
    script = commands[1][-1]
    active = script.split("ACTIVE_REPOS = ")[1].split("\n")[0]
    assert sorted(ast.literal_eval(active)) == ["repo-1", "repo-2"]


def test_remote_residue_scan_fails_closed_when_docker_is_unreachable(tmp_path):
    """A dead docker daemon must fail the scan, never report a clean target.

    The command is the one shipped to the target, run here by a real `sh` with a
    `docker` that fails the way an unreachable daemon does. Reporting nothing and
    exiting 0 is the false "cleanup fully complete" this card exists to remove.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    docker = bin_dir / "docker"
    docker.write_text(
        "#!/bin/sh\necho 'Cannot connect to the Docker daemon' >&2\nexit 1\n",
    )
    docker.chmod(0o755)
    service_base = tmp_path / "services"
    (service_base / (_ORPHAN)).mkdir(parents=True)
    command = clean_live_tests.build_remote_residue_command(
        clean_live_tests.DEPLOY_SLUG_PREFIXES, service_base=str(service_base)
    )

    result = subprocess.run(
        shlex.split(command),
        capture_output=True,
        text=True,
        env={**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"},
        timeout=10,
    )

    assert result.returncode != 0
    assert "Cannot connect to the Docker daemon" in result.stderr
    # And the directory half must not have quietly answered for the container half.
    assert result.stdout == ""


def test_remote_residue_scan_reports_containers_and_directories(tmp_path):
    """The same command, with a working docker, still inventories both halves."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    docker = bin_dir / "docker"
    docker.write_text(f"#!/bin/sh\necho {_ORPHAN.replace('-', '_')}_backend_1\n")
    docker.chmod(0o755)
    service_base = tmp_path / "services"
    (service_base / _ORPHAN).mkdir(parents=True)
    command = clean_live_tests.build_remote_residue_command(
        clean_live_tests.DEPLOY_SLUG_PREFIXES, service_base=str(service_base)
    )

    result = subprocess.run(
        shlex.split(command),
        capture_output=True,
        text=True,
        env={**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"},
        timeout=10,
    )

    assert result.returncode == 0
    assert clean_live_tests.parse_remote_residue(result.stdout) == [
        f"container {_ORPHAN.replace('-', '_')}_backend_1",
        f"directory {service_base}/{_ORPHAN}",
    ]


def test_remote_server_list_failure_is_not_empty_list(monkeypatch):
    monkeypatch.setenv("INTERNAL_API_KEY", "test-internal-key")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers.get("X-Internal-Key") == "test-internal-key"
        return httpx.Response(500, text="db broke")

    transport = httpx.MockTransport(handler)
    original_client = httpx.Client

    def client_factory(*args, **kwargs):
        kwargs["transport"] = transport
        return original_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "Client", client_factory)

    with pytest.raises(clean_live_tests.CleanupFailure, match="server list fetch failed: 500"):
        clean_live_tests.clean_remote_servers(["live-te-11111111111111111111111111111111"])


_ORPHAN = "live-te-" + "a" * 32


def test_deploy_slug_prefixes_are_the_slugs_projects_actually_deploy_under():
    """The sweep's prefix is derived from slug generation, not copied by hand."""
    import uuid

    from shared.project_slug import generate_project_slug

    for title_prefix, slug_prefix in zip(
        clean_live_tests.PROJECT_PREFIXES, clean_live_tests.DEPLOY_SLUG_PREFIXES, strict=True
    ):
        slug = generate_project_slug(f"{title_prefix}-abc12345", uuid.uuid4())
        assert slug.startswith(slug_prefix)


def test_stack_names_are_recovered_from_containers_and_directories():
    """Every reported artefact resolves to a name the sweep can then remove."""
    findings = [
        f"container {_ORPHAN}-backend-1",
        f"container {_ORPHAN.replace('-', '_')}_postgres_1",
        f"directory /opt/services/{_ORPHAN}",
        "container unrelated-service-1",
        # A directory is the stack name itself, whatever it looks like.
        "directory /opt/services/live-te-leftover",
    ]

    assert clean_live_tests.stack_names_from_residue(findings) == {
        _ORPHAN,
        "live-te-leftover",
    }


def test_orphan_stack_with_no_database_row_is_swept(monkeypatch):
    """The sweep cleans what the target reports, not only what the DB lists.

    `get_test_projects()` can only name slugs whose project rows survived, so an
    orphan whose rows a previous cleanup already deleted was reachable by no step
    at all: not by the manifest (its run never recorded it), not by the DB list.
    """
    server = {"handle": "vps-1", "public_ip": "203.0.113.7", "ssh_user": "dev"}
    monkeypatch.setattr(clean_live_tests, "_fetch_remote_servers", lambda: [server])
    monkeypatch.setattr(clean_live_tests, "_fetch_remote_server_key", lambda handle: "KEY")
    residue_command = clean_live_tests.build_remote_residue_command(
        clean_live_tests.DEPLOY_SLUG_PREFIXES
    )
    commands: list[str] = []

    def fake_run(argv, **kwargs):
        command = argv[-1]
        commands.append(command)
        if command == residue_command:
            return _result(
                stdout=f"container {_ORPHAN}-backend-1\ndirectory /opt/services/{_ORPHAN}\n"
            )
        return _result(stdout="")

    monkeypatch.setattr(clean_live_tests.subprocess, "run", fake_run)

    # The database knows nothing about this stack any more.
    clean_live_tests.clean_remote_servers([])

    assert f"sh -s -- {_ORPHAN} /opt/services" in commands


def test_remote_residue_scan_failure_is_not_a_clean_target(monkeypatch):
    server = {"handle": "vps-1", "public_ip": "203.0.113.7", "ssh_user": "dev"}
    monkeypatch.setattr(clean_live_tests, "_fetch_remote_servers", lambda: [server])
    monkeypatch.setattr(clean_live_tests, "_fetch_remote_server_key", lambda handle: "KEY")
    monkeypatch.setattr(
        clean_live_tests.subprocess,
        "run",
        lambda argv, **kwargs: _result(returncode=255, stderr="ssh: connect refused"),
    )

    with pytest.raises(clean_live_tests.CleanupFailure, match="remote residue scan failed"):
        clean_live_tests.collect_remote_residue()


def test_collect_remote_residue_reports_findings_per_server(monkeypatch):
    servers = [
        {"handle": "vps-1", "public_ip": "203.0.113.7", "ssh_user": "dev"},
        {"handle": "vps-2", "public_ip": "203.0.113.8", "ssh_user": "runner"},
    ]
    monkeypatch.setattr(clean_live_tests, "_fetch_remote_servers", lambda: servers)
    monkeypatch.setattr(clean_live_tests, "_fetch_remote_server_key", lambda handle: "KEY")

    def fake_run(argv, **kwargs):
        if argv[argv.index("BatchMode=yes") + 1] == "dev@203.0.113.7":
            return _result(stdout=f"directory /opt/services/{_ORPHAN}\n")
        return _result(stdout="")

    monkeypatch.setattr(clean_live_tests.subprocess, "run", fake_run)

    assert clean_live_tests.collect_remote_residue() == {
        "vps-1": [f"directory /opt/services/{_ORPHAN}"]
    }


def test_recover_manifests_removes_proven_orphan(monkeypatch, tmp_path):
    manifest = tmp_path / ".live-manifests" / "orphan.json"
    manifest.parent.mkdir()
    manifest.write_text('{"run_id": "orphan", "resources": []}')
    monkeypatch.setattr(clean_live_tests, "ORCHESTRATOR_ROOT", str(tmp_path))

    clean_live_tests.recover_ownership_manifests()
    assert not manifest.exists()


def test_recover_manifests_keeps_unproven_resources(monkeypatch, tmp_path):
    _live_harness_modules()  # recovery rebuilds a real context before any phase runs
    manifest = tmp_path / ".live-manifests" / "run.json"
    manifest.parent.mkdir()
    manifest.write_text(
        '{"run_id":"run","resources":[{"kind":"github_repository","identifier":"org/repo"}]}'
    )
    monkeypatch.setattr(clean_live_tests, "ORCHESTRATOR_ROOT", str(tmp_path))
    # Recovery has three steps now: the fence, which needs the live API, the
    # run-scoped label sweep, which needs a daemon, and the manifest round-trip,
    # which needs the API again. All are stood in for; what is under test is that
    # an unproven resource is reported and its manifest kept.
    monkeypatch.setattr(clean_live_tests, "fence_run_work", lambda ctx: [])
    monkeypatch.setattr(clean_live_tests, "cleanup_run_scoped_resources", lambda run_id: [])
    monkeypatch.setattr(
        clean_live_tests,
        "cleanup_manifest_resources",
        lambda ctx: ["github_repository org/repo"],
    )

    with pytest.raises(clean_live_tests.CleanupFailure, match="github_repository org/repo"):
        clean_live_tests.recover_ownership_manifests()
    assert manifest.exists()


def test_recover_manifests_sweeps_the_runs_label_before_its_context(monkeypatch, tmp_path):
    """The label sweep runs for every manifest, and its failure is reported too.

    It goes ahead of the manifest round-trip because it needs nothing but the run
    id: a run whose context cannot be reconstructed at all still has its
    containers, sidecars and networks removed. It goes behind the fence, which
    is the phase before it.
    """
    manifest = tmp_path / ".live-manifests" / "run.json"
    manifest.parent.mkdir()
    manifest.write_text('{"run_id":"live-abc","resources":[{"kind":"project","identifier":"p-1"}]}')
    monkeypatch.setattr(clean_live_tests, "ORCHESTRATOR_ROOT", str(tmp_path))
    _live_harness_modules()  # recovery rebuilds a real context before any phase runs
    order: list[str] = []

    def sweep(run_id):
        order.append(f"sweep {run_id}")
        return ["run live-abc cleanup failed: containers remain"]

    monkeypatch.setattr(clean_live_tests, "fence_run_work", lambda ctx: order.append("fence") or [])
    monkeypatch.setattr(clean_live_tests, "cleanup_run_scoped_resources", sweep)
    monkeypatch.setattr(clean_live_tests, "cleanup_manifest_resources", lambda ctx: [])

    with pytest.raises(clean_live_tests.CleanupFailure, match="containers remain"):
        clean_live_tests.recover_ownership_manifests()

    assert order == ["fence", "sweep live-abc"]
    assert manifest.exists()


def test_recover_manifests_removes_nothing_when_the_fence_cannot_be_established(
    monkeypatch, tmp_path
):
    """No fence, no capture and no removal — and a loud failure, not a quiet skip.

    Refusing to clean is the right answer in exactly this case and no other: the
    run cannot be stopped, so nothing may be read from it or taken away from it.
    """
    _live_harness_modules()  # recovery rebuilds a real context before any phase runs
    manifest = tmp_path / ".live-manifests" / "run.json"
    manifest.parent.mkdir()
    manifest.write_text('{"run_id":"live-abc","resources":[{"kind":"project","identifier":"p-1"}]}')
    monkeypatch.setattr(clean_live_tests, "ORCHESTRATOR_ROOT", str(tmp_path))
    touched: list[str] = []

    monkeypatch.setattr(
        clean_live_tests, "fence_run_work", lambda ctx: ["run live-abc fence: RuntimeError: no API"]
    )
    monkeypatch.setattr(
        clean_live_tests,
        "cleanup_run_scoped_resources",
        lambda run_id: touched.append("sweep") or [],
    )
    monkeypatch.setattr(
        clean_live_tests,
        "cleanup_manifest_resources",
        lambda ctx: touched.append("manifest") or [],
    )

    with pytest.raises(clean_live_tests.CleanupFailure, match="fence: RuntimeError: no API"):
        clean_live_tests.recover_ownership_manifests()

    assert touched == []
    assert manifest.exists()


def _running_container(worker_id: str, *, running: bool) -> dict:
    """One `docker inspect` answer, for a worker that is or is not still working."""
    return {
        "State": {
            "Status": "running" if running else "exited",
            "Running": running,
            "OOMKilled": False,
            "StartedAt": "2026-08-13T20:00:00Z",
            "FinishedAt": "" if running else "2026-08-13T20:05:00Z",
            "Error": "",
            "ExitCode": 0 if running else 137,
        },
        "Config": {
            "Env": ["WORKER_TYPE=developer", f"WORKER_ID={worker_id}"],
            "Image": "codegen/worker:test",
        },
        "Image": "sha256:0000",
        "Created": "2026-08-13T20:00:00Z",
        "Mounts": [],
    }


def test_recovery_fences_the_live_target_run_before_it_captures_or_removes_it(
    monkeypatch, tmp_path
):
    """The harness crashed; its run did not. Recovery stops it before touching it.

    Losing the harness stops nothing else: the API, the scheduler and
    worker-manager carry the project on, so a worker carrying the target run's
    label is still working when an operator runs the ordinary cleanup. This
    drives the real `recover_ownership_manifests` over a fake API and a fake
    daemon whose worker keeps running until its run is cancelled, and holds the
    four phases to their order — fence, capture, remove, verify.

    The neighbouring-run case in `tests/integration/backend/test_run_scoped_cleanup.py`
    cannot catch this: it kills every worker before the cleanup, so it proves
    label isolation against a dead neighbour, not safety against a live target.
    """
    pipeline_helpers, run_cleanup, run_evidence = _live_harness_modules()

    manifest = tmp_path / ".live-manifests" / "live-abc.json"
    manifest.parent.mkdir()
    manifest.write_text(
        json.dumps({"run_id": "live-abc", "resources": [{"kind": "project", "identifier": "p-1"}]})
    )
    monkeypatch.setattr(clean_live_tests, "ORCHESTRATOR_ROOT", str(tmp_path))
    monkeypatch.setattr(pipeline_helpers, "ORCHESTRATOR_ROOT", tmp_path)

    order: list[str] = []
    run = {"id": "live-abc", "project_id": "p-1", "status": "running"}

    def api(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/api/runs/":
            return httpx.Response(200, json=[run])
        if request.method == "PATCH" and request.url.path == "/api/runs/live-abc":
            order.append("cancel")
            # Quiescence: the worker this run started stops with it.
            run["status"] = "cancelled"
            return httpx.Response(200, json=run)
        raise AssertionError(f"unexpected {request.method} {request.url.path}")

    monkeypatch.setattr(
        clean_live_tests,
        "internal_api_client",
        lambda: httpx.AsyncClient(base_url="http://api", transport=httpx.MockTransport(api)),
    )
    # The consumer half of the fence talks to Redis through `docker compose exec`.
    # What it does there is held elsewhere; that it happens at all is here.
    for name in ("cancel_owned_scaffold", "cancel_owned_active_work"):
        monkeypatch.setattr(pipeline_helpers, name, lambda ctx: order.append("fence consumers"))
    monkeypatch.setattr(pipeline_helpers, "cleanup_owned_capability_work", lambda ctx: None)

    worker = run_cleanup.LabelledResource(
        name="worker-w1", kind="worker", worker_id="w1", run_id="live-abc"
    )
    containers = {worker.name: worker}

    def remove_container(name: str):
        order.append("remove" if run["status"] == "cancelled" else "remove while the run is live")
        containers.pop(name, None)

    ops = run_cleanup.CleanupOps(
        list_containers=lambda run_id: [
            item for item in containers.values() if item.run_id == run_id
        ],
        remove_container=remove_container,
        list_networks=lambda run_id: [],
        remove_network=lambda name: None,
        meta_workers=lambda run_id: [],
        delete_keys=lambda names: None,
        existing_keys=lambda names: [],
    )
    monkeypatch.setattr(run_cleanup, "docker_cli_ops", lambda root, **kwargs: ops)

    def probe(root=None):
        def list_run_workers(run_id):
            order.append("capture")
            return [
                run_evidence.ListedWorker(container=item.name, worker_id=item.worker_id)
                for item in containers.values()
                if item.run_id == run_id
            ]

        return run_evidence.ContainerProbe(
            list_run_workers=list_run_workers,
            # The worker is doing this run's work until the fence cancels it.
            inspect=lambda container: _running_container(
                "w1", running=run["status"] != "cancelled"
            ),
            logs=lambda container, tail: "",
            removed_workers=lambda run_id: [],
        )

    monkeypatch.setattr(run_evidence, "docker_probe", probe)
    # The manifest round-trip needs the whole live stack; the phases under test
    # are the three in front of it.
    monkeypatch.setattr(
        clean_live_tests,
        "cleanup_manifest_resources",
        lambda ctx: order.append("manifest round-trip") or [],
    )

    clean_live_tests.recover_ownership_manifests()

    assert order == [
        "fence consumers",  # scaffold
        "cancel",
        "fence consumers",  # the capability consumers, behind the cancelled run
        "capture",
        "remove",
        "manifest round-trip",
    ]
    assert containers == {}

    # And the evidence proves the fence really came first: the capture read a
    # worker that had already stopped, so its ending is a fact rather than
    # "still running when this was captured".
    artifact = json.loads((tmp_path / ".live-manifests" / "evidence" / "live-abc.json").read_text())
    (record,) = artifact["workers"]
    assert record["worker_id"] == "w1"
    assert record["exit_code"]["status"] == "captured"
    assert record["exit_code"]["value"] == 137


def test_main_remote_failure_leaves_db_slugs_available_for_retry(monkeypatch, tmp_path):
    monkeypatch.setattr(clean_live_tests, "ORCHESTRATOR_ROOT", str(tmp_path))
    projects = [
        {
            "id": "project-1",
            "title": "live-test-old",
            "slug": "live-te-11111111111111111111111111111111",
        }
    ]
    calls: list[tuple[str, object]] = []
    remote_attempts = 0

    monkeypatch.setattr(clean_live_tests, "recover_ownership_manifests", lambda: None)
    monkeypatch.setattr(clean_live_tests, "get_test_projects", lambda: projects)
    monkeypatch.setattr(
        clean_live_tests,
        "clean_redis_queues",
        lambda project_ids: calls.append(("redis", project_ids)),
    )
    monkeypatch.setattr(
        clean_live_tests,
        "delete_github_repos",
        lambda repo_names: calls.append(("github", repo_names)),
    )
    monkeypatch.setattr(
        clean_live_tests,
        "clean_database",
        lambda: calls.append(("database", None)),
    )
    monkeypatch.setattr(
        clean_live_tests,
        "clean_local_docker",
        lambda: calls.append(("local_docker", None)),
    )
    monkeypatch.setattr(
        clean_live_tests,
        "clean_local_workspaces",
        lambda: calls.append(("workspaces", None)),
    )
    monkeypatch.setattr(
        clean_live_tests,
        "verify_no_residue",
        lambda project_ids: calls.append(("verify", project_ids)),
    )

    def fake_remote(project_slugs):
        nonlocal remote_attempts
        remote_attempts += 1
        calls.append(("remote", list(project_slugs)))
        if remote_attempts == 1:
            raise clean_live_tests.CleanupFailure("ssh key fetch failed")

    monkeypatch.setattr(clean_live_tests, "clean_remote_servers", fake_remote)

    with pytest.raises(clean_live_tests.CleanupFailure, match="ssh key fetch failed"):
        clean_live_tests.main()

    assert ("database", None) not in calls
    assert calls[-1] == ("remote", ["live-te-11111111111111111111111111111111"])

    clean_live_tests.main()

    assert calls.count(("remote", ["live-te-11111111111111111111111111111111"])) == 2
    assert calls.index(("remote", ["live-te-11111111111111111111111111111111"])) < calls.index(
        ("database", None)
    )


def test_unprovable_manifest_still_lets_every_other_sweep_run(monkeypatch, tmp_path):
    """A manifest that cannot be proven clean fails the run — at the end.

    Manifest recovery is the first step, and it raises whenever a deploy record
    cannot be cleared (no server registered, for instance). Aborting there left
    every later sweep unrun and hand-deleting the manifest as the only way out,
    while the operator still got a red run either way. Fail-closed is kept: the
    same failure is raised, after the sweeps that can still do their work.
    """
    monkeypatch.setattr(clean_live_tests, "ORCHESTRATOR_ROOT", str(tmp_path))
    calls: list[str] = []

    def failing_recovery():
        raise clean_live_tests.CleanupFailure("run.json: no target for an owned deploy")

    monkeypatch.setattr(clean_live_tests, "recover_ownership_manifests", failing_recovery)
    monkeypatch.setattr(clean_live_tests, "get_test_projects", list)
    for name in (
        "clean_redis_queues",
        "delete_github_repos",
        "clean_remote_servers",
        "verify_no_residue",
    ):
        monkeypatch.setattr(clean_live_tests, name, lambda *args, _name=name: calls.append(_name))
    for name in ("clean_database", "clean_local_docker", "clean_local_workspaces"):
        monkeypatch.setattr(clean_live_tests, name, lambda _name=name: calls.append(_name))

    with pytest.raises(clean_live_tests.CleanupFailure, match="no target for an owned deploy"):
        clean_live_tests.main()

    assert calls == [
        "clean_redis_queues",
        "delete_github_repos",
        "clean_remote_servers",
        "clean_database",
        "clean_local_docker",
        "clean_local_workspaces",
    ]


def _reloaded_for_contour(monkeypatch, contour):
    """Re-import the sweep as it would start in `contour`.

    The prefixes are module-level, resolved once at import, so the contour has to
    be in the environment before the import — exactly as when the process starts.
    """
    import importlib

    monkeypatch.setenv("LIVE_CONTOUR", contour)
    return importlib.reload(clean_live_tests)


def test_stand_sweep_cannot_address_production_projects(monkeypatch):
    """The whole boundary while both contours share one GitHub organization.

    A stand sweep must not know production's names — not match them and decline,
    but not carry them at all, in the SQL it builds and in the stack names it
    recognises.
    """
    try:
        module = _reloaded_for_contour(monkeypatch, "stand")

        assert module.PROJECT_PREFIXES == ["stand-test", "stand-crud"]
        assert module.DEPLOY_SLUG_PREFIXES == ["stand-t-", "stand-c-"]

        conditions = module._build_conditions()
        assert "stand-test-%" in conditions
        assert "live-test" not in conditions
        assert "mega-test" not in conditions

        assert not module._STACK_NAME_PATTERN.match("live-te-" + "0" * 32)
        assert module._STACK_NAME_PATTERN.match("stand-t-" + "0" * 32)
    finally:
        monkeypatch.delenv("LIVE_CONTOUR", raising=False)
        import importlib

        importlib.reload(clean_live_tests)


def test_production_sweep_is_unchanged_without_a_contour(monkeypatch):
    """No `LIVE_CONTOUR` in the environment means the sweep production has always run."""
    monkeypatch.delenv("LIVE_CONTOUR", raising=False)
    import importlib

    module = importlib.reload(clean_live_tests)

    assert module.PROJECT_PREFIXES == ["live-test", "live-crud", "mega-test"]
    assert module.DEPLOY_SLUG_PREFIXES == ["live-te-", "live-cr-", "mega-te-"]
    assert not module._STACK_NAME_PATTERN.match("stand-t-" + "0" * 32)


def test_a_repository_whose_project_row_is_gone_is_still_residue():
    """The database-driven half cannot see it, and it is the common leftover.

    A run killed between deleting its rows and deleting its repository leaves
    exactly this, and production's own sync then alerts about the orphan forever
    because it shares the organization.
    """
    residue = clean_live_tests.contour_repo_residue(
        [
            "live-te-" + "0" * 32,
            "fortune-teller-bot",
            "service-template",
            "live-cr-" + "1" * 32,
        ]
    )

    assert residue == ["live-cr-" + "1" * 32, "live-te-" + "0" * 32]


def test_a_repository_that_merely_starts_like_a_test_project_is_not_residue():
    """Deleting someone's repository because its name rhymes is unrecoverable."""
    assert clean_live_tests.contour_repo_residue(["live-team-notes", "live-test-docs"]) == []


def test_another_contour_is_not_this_contour_s_residue(monkeypatch):
    try:
        module = _reloaded_for_contour(monkeypatch, "stand")

        assert module.contour_repo_residue(["live-te-" + "0" * 32]) == []
        assert module.contour_repo_residue(["stand-t-" + "0" * 32]) == ["stand-t-" + "0" * 32]
    finally:
        monkeypatch.delenv("LIVE_CONTOUR", raising=False)
        import importlib

        importlib.reload(clean_live_tests)


def test_the_sweep_never_deletes_from_the_append_only_ledger():
    """The ledger is append-only by a database rule, and that rule is deliberate.

    Deleting the referenced user unconditionally made the sweep raise — and a
    raising sweep is not a partial one: every phase after the database went
    unrun. The user is a fixture reused by every run, so it goes only while
    nothing points at it.
    """
    import inspect

    source = inspect.getsource(clean_live_tests.clean_database)

    assert "DELETE FROM engineering_attempt_ledger" not in source
    assert "NOT EXISTS (SELECT 1 FROM engineering_attempt_ledger" in source
