"""Fixed operation argv and refusal precede product mutation."""

from datetime import UTC, datetime
import json
import subprocess
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from shared.contracts.queues.scaffold import ScaffoldMessage
from shared.workspace_preservation import CATALOG_INSTALL_ATTEMPTS
from src.install import (
    InstallExecutionError,
    attempt_checkout,
    install_environment,
    product_environment,
    protected_files,
    run_install,
)


def test_product_environment_discards_inherited_credentials(tmp_path, monkeypatch):
    for name in ("GIT_CONFIG_VALUE_0", "GIT_CONFIG_VALUE_1", "GITHUB_TOKEN", "SECRET_KEY"):
        monkeypatch.setenv(name, "synthetic-parent-secret")
    env = product_environment(tmp_path)
    # The only git configuration a product command gets is trust in its own checkout.
    assert {key: value for key, value in env.items() if key.startswith("GIT_CONFIG_")} == {
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "safe.directory",
        "GIT_CONFIG_VALUE_0": str(tmp_path),
    }
    assert "synthetic-parent-secret" not in env.values()


def test_bot_owned_sources_are_protected_and_generated_output_is_mutable(tmp_path):
    tracked = [
        "services/tg_bot/src/main.py",
        "services/tg_bot/src/menu.py",
        "services/tg_bot/src/generated/bindings.py",
    ]
    for name in tracked:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("baseline\n")
    assert set(protected_files(tmp_path, tracked)) == set(tracked[:2])


@pytest.mark.subprocess
@pytest.mark.parametrize("suffix", ["", ".git"])
@pytest.mark.parametrize(
    "foreign",
    [
        "https://github.com/vladmesh/codegen-product-kit.git",
        "https://github.com/owner/notes-fork.git",
    ],
)
def test_product_auth_does_not_reach_the_public_catalog(tmp_path, suffix, foreign):
    git_url = "https://github.com/owner/notes"
    env = install_environment("synthetic-token", tmp_path, git_url)
    owned = subprocess.run(
        ["git", "config", "--get-urlmatch", "http.extraheader", git_url + suffix],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert owned.returncode == 0 and owned.stdout.startswith("Authorization: Basic ")
    catalog = subprocess.run(
        [
            "git",
            "config",
            "--get-urlmatch",
            "http.extraheader",
            foreign,
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert catalog.returncode == 1 and catalog.stdout == ""


def message():
    return ScaffoldMessage(
        project_id="00000000-0000-0000-0000-000000000001",
        repository_id="repo-1",
        mode="install",
        template_repo="gh:vladmesh/codegen-product-kit",
        template_ref="release-test",
        project_name="notes",
        modules="backend,tg_bot",
        task_id="task-1",
        story_id="story-1",
        operation_id="install-1",
        cycle_started_at=datetime.now(UTC),
        install={
            "package": {
                "name": "reminders",
                "distribution": "codegen-kit-reminders",
                "version": "0.5.0",
                "tag": "packages/reminders/v0.5.0",
            },
            "libraries": [
                {
                    "name": "textparse",
                    "distribution": "codegen-kit-textparse",
                    "version": "0.1.0",
                    "tag": "packages/textparse/v0.1.0",
                }
            ],
            "binding": {
                "package": "reminders",
                "resource": "codegen_kit_reminders:bindings/default.yaml",
                "sha256": "a" * 64,
                "functions": ["textparse.when"],
            },
            "core_version": "2.2.0",
            "python_version": "3.12.0",
            "catalog_digest": "b" * 64,
            "tooling_commit": "c" * 40,
            "catalog": {
                "repository": "https://github.com/vladmesh/codegen-product-kit.git",
                "commit": "d" * 40,
                "catalog_sha256": "e" * 64,
            },
        },
    )


@pytest.mark.asyncio
async def test_missing_workspace_refuses_without_any_command(tmp_path, monkeypatch):
    command = AsyncMock()
    monkeypatch.setattr("src.install._run_cmd", command)
    with pytest.raises(InstallExecutionError, match="workspace_unowned"):
        await run_install(
            message(),
            SimpleNamespace(workspace_base_path=str(tmp_path)),
            "https://github.com/owner/notes",
            "fake-token",
            AsyncMock(),
        )
    command.assert_not_awaited()


def test_an_attempt_checkout_is_derived_from_the_operation_never_supplied(tmp_path):
    name, path = attempt_checkout(str(tmp_path), "repo-1", "install-1")
    assert name == "repo-1/install-1"
    assert path == tmp_path.resolve() / CATALOG_INSTALL_ATTEMPTS / "repo-1" / "install-1"
    for repository, operation in (("repo-1", ".."), ("..", "install-1"), ("repo-1", "a/b")):
        with pytest.raises(InstallExecutionError, match="attempt_unowned"):
            attempt_checkout(str(tmp_path), repository, operation)


@pytest.mark.asyncio
async def test_a_retained_attempt_is_never_reused_by_its_operation(tmp_path, monkeypatch):
    """A redelivered operation finds its own earlier checkout and runs nothing in it."""
    (tmp_path / "repo-1/.git").mkdir(parents=True)
    retained = tmp_path / CATALOG_INSTALL_ATTEMPTS / "repo-1/install-1"
    retained.mkdir(parents=True)
    (retained / "evidence.txt").write_text("failed attempt\n")
    command = AsyncMock()
    monkeypatch.setattr("src.install._run_cmd", command)
    with pytest.raises(InstallExecutionError, match="attempt_exists") as refused:
        await run_install(
            message(),
            SimpleNamespace(workspace_base_path=str(tmp_path)),
            "https://github.com/owner/notes",
            "fake-token",
            AsyncMock(),
        )
    assert refused.value.stage == "prepare"
    command.assert_not_awaited()
    assert (retained / "evidence.txt").read_text() == "failed attempt\n"


def foreign_owned_checkout(root):
    """A real checkout git treats as owned by another user, as a worker-owned workspace is."""
    subprocess.run(["git", "init", "--quiet", str(root)], check=True)
    (root / "notes.py").write_text("dirty\n")
    return {"GIT_TEST_ASSUME_DIFFERENT_OWNER": "1"}


@pytest.mark.asyncio
async def test_install_git_trusts_the_worker_owned_workspace(tmp_path, monkeypatch):
    root = tmp_path / "repo-1"
    foreign = foreign_owned_checkout(root)
    real_run_cmd = run_install.__globals__["_run_cmd"]

    async def as_root_on_worker_checkout(args, *, env, **kwargs):
        return await real_run_cmd(args, env=env | foreign, **kwargs)

    monkeypatch.setattr("src.install._run_cmd", as_root_on_worker_checkout)
    # Trusted, git reads the workspace and finds no owned origin; untrusted, it would
    # refuse the repository as dubious before reading anything.
    with pytest.raises(InstallExecutionError) as refused:
        await run_install(
            message(),
            SimpleNamespace(workspace_base_path=str(tmp_path)),
            "https://github.com/owner/notes",
            "fake-token",
            AsyncMock(),
        )
    assert "origin" in str(refused.value) and "dubious" not in str(refused.value)


@pytest.mark.parametrize("build", ["product", "install"])
def test_product_tool_git_trusts_the_worker_owned_workspace(tmp_path, build):
    root = tmp_path / "repo-1"
    foreign = foreign_owned_checkout(root)
    env = (
        product_environment(root)
        if build == "product"
        else install_environment("synthetic-token", root, "https://github.com/owner/notes")
    )
    status = subprocess.run(
        ["git", "-C", str(root), "status", "--porcelain"],
        env=env | foreign,
        capture_output=True,
        text=True,
        check=False,
    )
    assert status.returncode == 0, status.stderr


def check_install(status="mechanical", glue=(), incompatible=None, **target):
    """`kit check-install --json` as the pinned kit prints it for this message's closure."""
    install = message().install
    return {
        "result_version": 1,
        "package": install.package.name,
        "status": status,
        "product_core": install.core_version,
        "target": None
        if incompatible
        else {
            "route": "catalog",
            "catalog_source": install.catalog.repository,
            "catalog_ref": install.catalog.commit,
            "tag": install.package.tag,
            "version": install.package.version,
            "requires_core": ">=2.2,<3",
            "metadata_sha256": "f" * 64,
        }
        | target,
        "glue": list(glue),
        "incompatible": incompatible,
    }


def glue_item(code, *, owner="product", symbol=None, path="services/tg_bot/src/commands.py"):
    return {
        "code": code,
        "path": path,
        "line": 12,
        "owner": owner,
        "symbol": symbol,
        "key": None,
        "command": "remind",
        "conflict": f"{code} conflict",
        "action": f"resolve {code}",
        "other": None,
    }


def product_checkout_until(tmp_path, stop, preflight=None, preflight_rc=0):
    """A fake product checkout whose commands succeed until ``stop(argv)`` answers."""
    (tmp_path / "repo-1/.git").mkdir(parents=True)
    calls = []
    answer_preflight = check_install() if preflight is None else preflight

    async def command(args, **kwargs):
        calls.append(args)
        if (answer := stop(args)) is not None:
            return answer
        if "get-url" in args:
            return 0, "https://github.com/owner/notes\n", ""
        if "ls-remote" in args:
            return 0, "", ""
        if "rev-parse" in args:
            return 0, "d" * 40 + "\n", ""
        if "provenance" in args or "preflight" in args:
            return 0, "{}", ""
        if "check-install" in args:
            return preflight_rc, json.dumps(answer_preflight), ""
        return 0, "", ""

    return calls, command


async def execute(tmp_path, fence=None):
    return await run_install(
        message(),
        SimpleNamespace(workspace_base_path=str(tmp_path)),
        "https://github.com/owner/notes",
        "fake-token",
        fence or AsyncMock(),
    )


def kit_calls(calls):
    return [args[1:] for args in calls if args[0].endswith("/.venv/bin/kit")]


@pytest.mark.asyncio
async def test_the_attempt_checkout_starts_at_the_scaffold_base_and_is_prepared_by_the_kit(
    tmp_path, monkeypatch
):
    calls, command = product_checkout_until(
        tmp_path, lambda args: (2, "stop\n", "") if args[:1] == ["make"] else None
    )
    monkeypatch.setattr("src.install._run_cmd", command)
    fence = AsyncMock()
    with pytest.raises(InstallExecutionError):
        await execute(tmp_path, fence)
    attempt = tmp_path.resolve() / CATALOG_INSTALL_ATTEMPTS / "repo-1/install-1"
    git = ["git", "-c", "core.hooksPath=/dev/null"]
    # No remote story branch yet: the scaffold base, never a local branch of the workspace.
    assert [*git, "rev-parse", "--verify", "refs/remotes/origin/main^{commit}"] in calls
    assert [*git, "worktree", "add", "--detach", str(attempt), "d" * 40] in calls
    assert ["sh", "scripts/prepare-env.sh", "root", "backend", "tg_bot"] in calls
    checkouts = {call.args[0].checkout for call in fence.await_args_list} - {None}
    assert checkouts == {"repo-1/install-1"}


@pytest.mark.asyncio
async def test_an_existing_remote_story_head_is_the_attempt_base(tmp_path, monkeypatch):
    head = "e" * 40

    def remote(args):
        if "ls-remote" in args:
            return 0, f"{head}\trefs/heads/story/story-1\n", ""
        if args[:1] == ["make"]:
            return 2, "stop\n", ""
        return None

    calls, command = product_checkout_until(tmp_path, remote)
    monkeypatch.setattr("src.install._run_cmd", command)
    with pytest.raises(InstallExecutionError):
        await execute(tmp_path)
    assert [
        "git",
        "-c",
        "core.hooksPath=/dev/null",
        "rev-parse",
        "--verify",
        f"{head}^{{commit}}",
    ] in calls


@pytest.mark.asyncio
async def test_kit_preflight_runs_on_the_exact_saved_release_before_any_add(tmp_path, monkeypatch):
    calls, command = product_checkout_until(
        tmp_path, lambda args: (2, "stop\n", "") if args[:1] == ["make"] else None
    )
    monkeypatch.setattr("src.install._run_cmd", command)
    fence = AsyncMock()
    with pytest.raises(InstallExecutionError):
        await execute(tmp_path, fence)
    attempt = tmp_path.resolve() / CATALOG_INSTALL_ATTEMPTS / "repo-1/install-1"
    pinned = ["--catalog-source", "https://github.com/vladmesh/codegen-product-kit.git"]
    pinned += ["--catalog-ref", "d" * 40]
    assert kit_calls(calls)[0] == [
        "check-install",
        "reminders",
        "--json",
        *pinned,
        "--version",
        "0.5.0",
        "--product-root",
        str(attempt),
    ]
    assert kit_calls(calls)[1][:2] == ["add", "reminders"]
    saved = [call.args[0].preflight for call in fence.await_args_list if call.args[0].preflight]
    assert saved and saved[-1].status == "mechanical"


@pytest.mark.asyncio
async def test_the_closures_own_library_is_not_product_glue(tmp_path, monkeypatch):
    """The kit asks for `kit add textparse` first; the fixed closure adds exactly that."""
    preflight = check_install(
        "glue", [glue_item("library_required", owner="package:reminders", symbol="textparse")]
    )
    calls, command = product_checkout_until(
        tmp_path,
        lambda args: (2, "stop\n", "") if args[:1] == ["make"] else None,
        preflight,
        preflight_rc=3,
    )
    monkeypatch.setattr("src.install._run_cmd", command)
    with pytest.raises(InstallExecutionError, match="stop"):
        await execute(tmp_path)
    assert [args[:2] for args in kit_calls(calls)][1:] == [
        ["add", "reminders"],
        ["add", "textparse"],
        ["bind", "reminders"],
    ]


@pytest.mark.parametrize(
    "item",
    [
        glue_item("command_conflict", symbol="handle_remind"),
        glue_item("language_owner", path="services/tg_bot/src/settings.py"),
        glue_item("library_required", owner="package:reminders", symbol="dateparse"),
    ],
)
@pytest.mark.asyncio
async def test_product_glue_refuses_before_any_mutation_with_the_kits_exact_items(
    tmp_path, monkeypatch, item
):
    calls, command = product_checkout_until(
        tmp_path, lambda args: None, check_install("glue", [item]), preflight_rc=3
    )
    monkeypatch.setattr("src.install._run_cmd", command)
    with pytest.raises(InstallExecutionError) as refused:
        await execute(tmp_path)
    assert refused.value.stage == "preflight"
    assert str(refused.value).startswith("glue_required: ")
    assert f"{item['code']} at {item['path']}:12: {item['action']}" in str(refused.value)
    assert refused.value.preflight.glue[0].model_dump() == item
    assert [args[0] for args in kit_calls(calls)] == ["check-install"]
    assert not any(args[0] == "make" for args in calls)


@pytest.mark.asyncio
async def test_an_incompatible_release_is_a_typed_refusal(tmp_path, monkeypatch):
    reason = {"code": "core_range", "explanation": "reminders 0.5.0 requires core >=3"}
    calls, command = product_checkout_until(
        tmp_path, lambda args: None, check_install("incompatible", incompatible=reason), 4
    )
    monkeypatch.setattr("src.install._run_cmd", command)
    with pytest.raises(InstallExecutionError, match="preflight_incompatible: core_range") as no:
        await execute(tmp_path)
    assert no.value.preflight.incompatible.code == "core_range"
    assert [args[0] for args in kit_calls(calls)] == ["check-install"]


@pytest.mark.parametrize(
    ("answer", "rc", "refusal"),
    [
        ("not json", 0, "preflight_malformed"),
        (json.dumps({"result_version": 2}), 0, "preflight_malformed"),
        (json.dumps(check_install("glue")), 3, "preflight_malformed"),
        (json.dumps(check_install()), 3, "preflight_exit_mismatch"),
        (json.dumps(check_install(tag="packages/reminders/v0.4.0")), 0, "target.tag"),
        (json.dumps(check_install(catalog_ref="0" * 40)), 0, "target.catalog_ref"),
        (json.dumps(check_install(version="0.4.0")), 0, "target.version"),
        (json.dumps(check_install() | {"package": "tg-channels"}), 0, "package"),
        (json.dumps(check_install() | {"product_core": "2.1.0"}), 0, "product_core"),
    ],
)
@pytest.mark.asyncio
async def test_an_untrusted_preflight_refuses_before_any_write(
    tmp_path, monkeypatch, answer, rc, refusal
):
    calls, command = product_checkout_until(
        tmp_path, lambda args: (rc, answer, "") if "check-install" in args else None
    )
    monkeypatch.setattr("src.install._run_cmd", command)
    with pytest.raises(InstallExecutionError, match=refusal) as refused:
        await execute(tmp_path)
    assert refused.value.stage == "preflight"
    assert [args[0] for args in kit_calls(calls)] == ["check-install"]


@pytest.mark.asyncio
async def test_a_preflight_that_wrote_to_the_product_is_refused(tmp_path, monkeypatch):
    statuses = iter(["", " M uv.lock"])

    def status(args):
        if args[3:5] == ["status", "--porcelain"] and "--untracked-files=all" in args:
            return 0, next(statuses), ""
        return None

    calls, command = product_checkout_until(tmp_path, status)
    monkeypatch.setattr("src.install._run_cmd", command)
    with pytest.raises(InstallExecutionError, match="preflight_not_read_only"):
        await execute(tmp_path)
    assert [args[0] for args in kit_calls(calls)] == ["check-install"]


@pytest.mark.asyncio
async def test_product_unit_leg_never_reaches_the_orchestrator_redis(tmp_path, monkeypatch):
    # The product .env the Makefile exports names redis://redis:6379; on the scaffolder's
    # network that is the orchestrator Redis, and a bound product's tests block on it.
    calls, command = product_checkout_until(
        tmp_path, lambda args: (2, "tests failed\n", "") if args[:2] == ["make", "tests"] else None
    )
    monkeypatch.setattr("src.install._run_cmd", command)
    with pytest.raises(InstallExecutionError, match="tests failed"):
        await run_install(
            message(),
            SimpleNamespace(workspace_base_path=str(tmp_path)),
            "https://github.com/owner/notes",
            "fake-token",
            AsyncMock(),
        )
    assert calls[-1] == ["make", "tests", "REDIS_URL=redis://redis.invalid:6379"]


@pytest.mark.asyncio
async def test_timed_out_product_command_refuses_with_its_argv(tmp_path, monkeypatch):
    def hang(args):
        if args[:2] == ["make", "validate-specs"]:
            raise TimeoutError

    _, command = product_checkout_until(tmp_path, hang)
    monkeypatch.setattr("src.install._run_cmd", command)
    with pytest.raises(InstallExecutionError) as refused:
        await run_install(
            message(),
            SimpleNamespace(workspace_base_path=str(tmp_path)),
            "https://github.com/owner/notes",
            "fake-token",
            AsyncMock(),
        )
    assert refused.value.stage == "validate"
    assert str(refused.value) == "timeout: make validate-specs ran over 600 s"


@pytest.mark.asyncio
async def test_every_component_is_added_from_the_payloads_pinned_catalog(tmp_path, monkeypatch):
    """`kit add` never falls back to the kit's moving default branch."""
    calls, command = product_checkout_until(
        tmp_path, lambda args: (2, "stop\n", "") if args[:1] == ["make"] else None
    )
    monkeypatch.setattr("src.install._run_cmd", command)
    with pytest.raises(InstallExecutionError):
        await run_install(
            message(),
            SimpleNamespace(workspace_base_path=str(tmp_path)),
            "https://github.com/owner/notes",
            "fake-token",
            AsyncMock(),
        )
    adds = [args[1:] for args in calls if args[0].endswith("/.venv/bin/kit") and args[1] == "add"]
    pinned = [
        "--catalog-source",
        "https://github.com/vladmesh/codegen-product-kit.git",
        "--catalog-ref",
        "d" * 40,
    ]
    assert adds == [["add", "reminders", *pinned], ["add", "textparse", *pinned]]


@pytest.mark.asyncio
async def test_a_payload_without_a_catalog_commit_runs_nothing(tmp_path, monkeypatch):
    """A payload stored before installs named their catalog is refused before any command."""
    (tmp_path / "repo-1/.git").mkdir(parents=True)
    command = AsyncMock()
    monkeypatch.setattr("src.install._run_cmd", command)
    unpinned = message()
    unpinned.install.catalog = None
    with pytest.raises(InstallExecutionError, match="catalog_unpinned") as refused:
        await run_install(
            unpinned,
            SimpleNamespace(workspace_base_path=str(tmp_path)),
            "https://github.com/owner/notes",
            "fake-token",
            AsyncMock(),
        )
    assert refused.value.stage == "preflight"
    command.assert_not_awaited()


def probe_modes(calls):
    return [args[3] for args in calls if len(args) > 3 and args[2].endswith("install_probe.py")]


def order_of(calls):
    """Probe modes, the kit's check and the first add, in the order they ran."""
    steps = []
    for args in calls:
        if len(args) > 3 and args[2].endswith("install_probe.py"):
            steps.append(f"probe {args[3]}")
        elif args[0].endswith("/.venv/bin/kit"):
            steps.append(args[1])
    return steps


@pytest.mark.asyncio
async def test_provenance_precedes_the_kits_classifier_and_ownership_checks_follow_it(
    tmp_path, monkeypatch
):
    calls, command = product_checkout_until(
        tmp_path, lambda args: (2, "stop\n", "") if args[:1] == ["make"] else None
    )
    monkeypatch.setattr("src.install._run_cmd", command)
    with pytest.raises(InstallExecutionError):
        await execute(tmp_path)
    assert order_of(calls)[:4] == [
        "probe provenance",
        "check-install",
        "probe preflight",
        "add",
    ]


@pytest.mark.parametrize(
    "item",
    [
        glue_item(
            "binding_language_owner",
            symbol="tg-channels",
            path="services/tg_bot/bindings/reminders.yaml",
        ),
        glue_item("command_collision", symbol="handle_remind"),
    ],
)
@pytest.mark.asyncio
async def test_a_product_conflict_reaches_the_typed_answer_not_a_probe_exit(
    tmp_path, monkeypatch, item
):
    """The probe's own binding/command refusals cannot answer before the kit classifies."""

    def owned_probe(args):
        if len(args) > 3 and args[2].endswith("install_probe.py") and args[3] == "preflight":
            return 1, "", "ValueError: binding_owned: existing product binding differs"
        return None

    calls, command = product_checkout_until(
        tmp_path, owned_probe, check_install("glue", [item]), preflight_rc=3
    )
    monkeypatch.setattr("src.install._run_cmd", command)
    with pytest.raises(InstallExecutionError) as refused:
        await execute(tmp_path)
    assert refused.value.preflight is not None
    assert refused.value.preflight.glue[0].code == item["code"]
    assert probe_modes(calls) == ["provenance"]


@pytest.mark.asyncio
async def test_a_provenance_failure_is_never_classified_as_glue(tmp_path, monkeypatch):
    def tooling(args):
        if len(args) > 3 and args[2].endswith("install_probe.py") and args[3] == "provenance":
            return 1, "", "ValueError: tooling_incompatible: saved requirement differs"
        return None

    calls, command = product_checkout_until(tmp_path, tooling)
    monkeypatch.setattr("src.install._run_cmd", command)
    with pytest.raises(InstallExecutionError, match="tooling_incompatible") as refused:
        await execute(tmp_path)
    assert refused.value.preflight is None
    assert not any(args[0].endswith("/.venv/bin/kit") for args in calls)


@pytest.mark.asyncio
async def test_a_mechanical_release_still_meets_the_probes_ownership_refusals(
    tmp_path, monkeypatch
):
    def owned_probe(args):
        if len(args) > 3 and args[2].endswith("install_probe.py") and args[3] == "preflight":
            return 1, "", "ValueError: binding_conflict: command is already owned"
        return None

    calls, command = product_checkout_until(tmp_path, owned_probe)
    monkeypatch.setattr("src.install._run_cmd", command)
    with pytest.raises(InstallExecutionError, match="binding_conflict") as refused:
        await execute(tmp_path)
    assert refused.value.stage == "preflight"
    assert [args[1] for args in calls if args[0].endswith("/.venv/bin/kit")] == ["check-install"]
