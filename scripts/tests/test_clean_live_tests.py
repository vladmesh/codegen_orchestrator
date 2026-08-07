from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from scripts import clean_live_tests


def _result(stdout="0\n", returncode=0, stderr=""):
    return SimpleNamespace(stdout=stdout, returncode=returncode, stderr=stderr)


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
    manifest = tmp_path / ".live-manifests" / "run.json"
    manifest.parent.mkdir()
    manifest.write_text(
        '{"run_id":"run","resources":[{"kind":"github_repository","identifier":"org/repo"}]}'
    )
    monkeypatch.setattr(clean_live_tests, "ORCHESTRATOR_ROOT", str(tmp_path))
    monkeypatch.setattr(
        clean_live_tests,
        "cleanup_manifest_resources",
        lambda data: ["github_repository org/repo"],
    )

    with pytest.raises(clean_live_tests.CleanupFailure, match="github_repository org/repo"):
        clean_live_tests.recover_ownership_manifests()
    assert manifest.exists()


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
