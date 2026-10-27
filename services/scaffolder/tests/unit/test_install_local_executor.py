"""A committed package source runs the shared executor at its saved commit, or refuses."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from shared.contracts.dto.install_operation import verification_refusal
from shared.workspace_preservation import CATALOG_INSTALL_ATTEMPTS
from src.install import InstallContext, InstallExecutionError, run_install
from tests.unit.test_install_executor import (
    LOCAL,
    glue_item,
    source_check_install,
    source_probe_output,
)

URL = "https://github.com/owner/notes"
SOURCE_TREE = "3" * 40
INSTALLED = "f" * 40
BINDING = {"default_binding": "notes:bindings/default.yaml", "binding_sha256": "9" * 64}
TRACKED = ["pyproject.toml", "services/backend/uv.lock", "packages/notes/notes/package.yaml"]


def local_context(**changes):
    return InstallContext(
        **{
            "project_id": str(LOCAL.project_id),
            "repository_id": LOCAL.repository_id,
            "story_id": "story-1",
            "operation_id": "install-1",
            "template_ref": "release-test",
            "install": LOCAL,
        }
        | changes
    )


def attempt(tmp_path):
    return tmp_path.resolve() / CATALOG_INSTALL_ATTEMPTS / "repo-1/install-1"


def probe_output(mode, binding=False, **changes):
    output = source_probe_output(mode, **changes)
    if binding:
        output["source"] |= BINDING
        if output["installed"] is not None:
            output["installed"]["binding_sha256"] = BINDING["binding_sha256"]
    return output


def git(args):
    """The git subcommand and its arguments, past every `-c` option, or `None`."""
    if args[:3] != ["git", "-c", "core.hooksPath=/dev/null"]:
        return None
    step = args[3:]
    while step[:1] == ["-c"]:
        step = step[2:]
    return step


def local_product(tmp_path, stop=lambda args: None, *, binding=False, readback=None):
    """A fake attempt at the owned remote story head `LOCAL.commit`; ``stop`` may answer first."""
    (tmp_path / "repo-1/.git").mkdir(parents=True)
    root = attempt(tmp_path)
    state = {"remote": LOCAL.commit, "head": LOCAL.commit}
    calls = []
    readback = readback or probe_output("readback", binding)

    async def command(args, **kwargs):
        calls.append((args, kwargs["env"]))
        if (answer := stop(args)) is not None:
            return answer
        step = git(args) or []
        if step == ["remote", "get-url", "origin"]:
            return 0, URL + "\n", ""
        if step[:1] == ["ls-remote"]:
            return 0, f"{state['remote']}\trefs/heads/story/story-1\n", ""
        if step[:2] == ["worktree", "add"]:
            for item in [*TRACKED, ".venv/lib/site/x.py", "services/backend/.venv/lib/y.py"]:
                (root / item).parent.mkdir(parents=True, exist_ok=True)
                (root / item).write_text("baseline\n")
            (root / "services/tg_bot/.venv").mkdir(parents=True)
        if step == ["ls-files"]:
            return 0, "\n".join(TRACKED) + "\n", ""
        if step[:1] == ["rev-parse"]:
            target = step[-1]
            if target == f"HEAD:{LOCAL.path}":
                return 0, SOURCE_TREE + "\n", ""
            if target.startswith("HEAD"):
                return 0, state["head"] + "\n", ""
            return 0, target.removesuffix("^{commit}") + "\n", ""
        if step == ["status", "--porcelain"]:
            return 0, " M services/backend/uv.lock\n", ""
        if step[:1] == ["commit"]:
            state["head"] = INSTALLED
        if step[:1] == ["push"]:
            state["remote"] = state["head"]
        if "source-provenance" in args:
            return 0, json.dumps(probe_output("provenance", binding)), ""
        if "source-readback" in args:
            return 0, json.dumps(readback), ""
        if "check-install" in args:
            return 0, json.dumps(source_check_install()), ""
        return 0, "", ""

    return calls, command


class Fence:
    """The API's canonical transition: a local proof advances once, then only repeats.

    `log` keeps each command with how many product commands had run before it.
    """

    def __init__(self, calls=(), refuse=lambda command: False):
        self.calls, self.log, self.current, self.refuse = calls, [], None, refuse

    @property
    def commands(self):
        return [item for _, item in self.log]

    async def __call__(self, command):
        self.log.append((len(self.calls), command))
        if command.verification is not None:
            if refusal := verification_refusal(self.current, command.verification, LOCAL):
                raise InstallExecutionError(command.stage, refusal)
            self.current = command.verification
        if self.refuse(command):
            raise InstallExecutionError(command.stage, "lease_lost")


async def execute(tmp_path, command, monkeypatch, fence=None, context=None):
    monkeypatch.setattr("src.install._run_cmd", command)
    return await run_install(
        context or local_context(),
        SimpleNamespace(workspace_base_path=str(tmp_path)),
        URL,
        "synthetic-token",
        fence or Fence(),
    )


def steps(calls):
    """Probe modes, kit commands and repository writes, in the order they ran."""
    found = []
    for args, _ in calls:
        probe = next(
            (at for at, item in enumerate(args) if item.endswith("install_probe.py")), None
        )
        if probe is not None:
            found.append(args[probe + 1])
        elif args[0].endswith("/.venv/bin/kit"):
            found.append(" ".join(args[1:3]))
        elif (git(args) or [None])[0] in {"add", "commit", "push"}:
            found.append(git(args)[0])
    return found


@pytest.mark.asyncio
async def test_a_committed_source_installs_and_publishes_from_its_saved_path(tmp_path, monkeypatch):
    calls, command = local_product(tmp_path)
    fence = Fence()
    result = await execute(tmp_path, command, monkeypatch, fence)
    root = attempt(tmp_path)
    kit = str(root / ".venv/bin/kit")
    assert steps(calls) == [
        "source-provenance",
        "check-install notes",
        "add notes",
        "source-readback",
        "add",
        "commit",
        "push",
    ]
    argv = {args[1]: args for args, _ in calls if args[0] == kit}
    assert argv["check-install"] == [kit, "check-install", "notes", "--json"] + [
        "--package-source",
        "packages/notes",
        "--version",
        "1.0.0",
        "--product-root",
        str(root),
    ]
    assert argv["add"] == [kit, "add", "notes", "--package-source", "packages/notes"]
    staged = next(args for args, _ in calls if (git(args) or [None])[0] == "add")
    assert git(staged) == ["add", "-A", "--", ".", ":(exclude,literal)packages/notes"]
    # Source checks run without bytecode; product commands never carry the repository token.
    for args, env in calls:
        if "source-provenance" in args or "source-readback" in args or "check-install" in args:
            assert args[1:3] == ["-I", "-B"] or args[0] == kit
            assert env["PYTHONDONTWRITEBYTECODE"] == "1"
        authorized = any(value.startswith("Authorization:") for value in env.values())
        assert authorized == ((git(args) or [None])[0] in {"fetch", "ls-remote", "push"})
    assert (result.head_sha, result.base_sha) == (INSTALLED, LOCAL.commit)
    assert result.checkout_removed and result.evidence["mode"] == "readback"


@pytest.mark.asyncio
async def test_each_proof_is_checkpointed_before_the_step_it_admits(tmp_path, monkeypatch):
    calls, command = local_product(tmp_path)
    fence = Fence(calls)
    await execute(tmp_path, command, monkeypatch, fence)

    def first(mode):
        return next(
            at for at, item in fence.log if item.verification and item.verification.mode == mode
        )

    def ran(predicate):
        return next(at for at, (args, _) in enumerate(calls) if predicate(args))

    # The provenance is held before the kit reads the source, the readback before staging.
    assert first("provenance") <= ran(lambda args: "check-install" in args)
    assert first("readback") <= ran(lambda args: (git(args) or [None])[0] == "add")
    assert {item.base_sha for item in fence.commands} <= {None, LOCAL.commit}
    published = fence.commands[-1]
    assert published.action == "publish" and published.head_sha == INSTALLED
    assert published.verification.mode == "readback" and published.preflight.status == "mechanical"


@pytest.mark.parametrize("binding", [False, True])
@pytest.mark.asyncio
async def test_only_a_declared_default_binding_is_bound(tmp_path, monkeypatch, binding):
    calls, command = local_product(tmp_path, binding=binding)
    await execute(tmp_path, command, monkeypatch)
    kit = [args[1:] for args, _ in calls if args[0].endswith("/.venv/bin/kit")]
    assert (["bind", "notes", "--default"] in kit) is binding
    assert kit[1] == ["add", "notes", "--package-source", "packages/notes"]


@pytest.mark.parametrize(
    "changes", [{"project_id": "00000000-0000-4000-8000-000000000002"}, {"repository_id": "repo-2"}]
)
@pytest.mark.asyncio
async def test_a_source_of_another_project_or_repository_runs_nothing(
    tmp_path, monkeypatch, changes
):
    for repository in ("repo-1", "repo-2"):
        (tmp_path / repository / ".git").mkdir(parents=True)
    command = AsyncMock()
    with pytest.raises(InstallExecutionError, match="local_target_unowned") as refused:
        await execute(tmp_path, command, monkeypatch, context=local_context(**changes))
    assert refused.value.stage == "preflight"
    command.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_moved_story_head_refuses_for_replanning_before_any_attempt(tmp_path, monkeypatch):
    moved = "b" * 40

    def head(args):
        if (git(args) or [None])[0] == "ls-remote":
            return 0, f"{moved}\trefs/heads/story/story-1\n", ""
        return None

    calls, command = local_product(tmp_path, head)
    with pytest.raises(InstallExecutionError, match="source_base_mismatch") as refused:
        await execute(tmp_path, command, monkeypatch)
    assert (refused.value.stage, refused.value.base_sha) == ("prepare", moved)
    assert not any((git(args) or [None])[0] == "worktree" for args, _ in calls)
    assert not any(args[0] == "sh" for args, _ in calls)
    assert not attempt(tmp_path).exists()


@pytest.mark.parametrize(
    "item",
    [
        glue_item("command_conflict", symbol="handle_note"),
        glue_item("library_required", owner="package:notes", symbol="textparse"),
    ],
)
@pytest.mark.asyncio
async def test_any_source_glue_refuses_before_mutation(tmp_path, monkeypatch, item):
    def glue(args):
        if "check-install" in args:
            return 3, json.dumps(source_check_install("glue", [item])), ""
        return None

    calls, command = local_product(tmp_path, glue)
    with pytest.raises(InstallExecutionError, match="glue_required") as refused:
        await execute(tmp_path, command, monkeypatch)
    assert refused.value.preflight.glue[0].code == item["code"]
    assert steps(calls) == ["source-provenance", "check-install notes"]


@pytest.mark.parametrize(
    "changed",
    [
        "packages/notes/notes/package.yaml",
        "services/backend/uv.lock",
        ".venv/lib/site/__pycache__/x.cpython-312.pyc",
    ],
)
@pytest.mark.asyncio
async def test_a_source_check_that_writes_any_byte_refuses(tmp_path, monkeypatch, changed):
    """An existing cache or lock byte counts, not only installed distribution names."""

    def write(args):
        if "check-install" in args:
            path = attempt(tmp_path) / changed
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("written by a check\n")

    calls, command = local_product(tmp_path, write)
    with pytest.raises(InstallExecutionError, match="preflight_not_read_only"):
        await execute(tmp_path, command, monkeypatch)
    assert steps(calls) == ["source-provenance", "check-install notes"]


@pytest.mark.asyncio
async def test_a_source_check_that_writes_an_environment_names_the_tree_and_paths(
    tmp_path, monkeypatch
):
    cache = "lib/python3.12/site-packages/__pycache__"
    written = [f"{cache}/_virtualenv.cpython-312.pyc"]
    written += [f"{cache}/module{at}.cpython-312.pyc" for at in range(5)]

    def write(args):
        if "source-provenance" in args:
            for item in written:
                path = attempt(tmp_path) / "services/tg_bot/.venv" / item
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"cache")

    calls, command = local_product(tmp_path, write)
    with pytest.raises(InstallExecutionError) as refused:
        await execute(tmp_path, command, monkeypatch)
    assert str(refused.value) == (
        "preflight_not_read_only: the product changed under preflight: tree:tg_bot ("
        + ", ".join(sorted(written)[:5])
        + ", +1 more)"
    )
    assert refused.value.stage == "preflight"


@pytest.mark.asyncio
async def test_a_protected_byte_changed_by_the_install_refuses_before_staging(
    tmp_path, monkeypatch
):
    def rewrite(args):
        if args[0].endswith("/.venv/bin/kit") and args[1] == "add":
            (attempt(tmp_path) / "pyproject.toml").write_text("rewritten\n")

    calls, command = local_product(tmp_path, rewrite)
    with pytest.raises(InstallExecutionError, match="protected_files_changed: pyproject.toml"):
        await execute(tmp_path, command, monkeypatch)
    assert "add" not in steps(calls)
    assert attempt(tmp_path).is_dir()


@pytest.mark.asyncio
async def test_remaining_source_residue_is_refused_and_retained_for_review(tmp_path, monkeypatch):
    residue = probe_output("readback", residue=["packages/notes/build/lib/notes/__init__.py"])
    calls, command = local_product(tmp_path, readback=residue)
    with pytest.raises(InstallExecutionError, match="source_residue: .*build/lib") as refused:
        await execute(tmp_path, command, monkeypatch)
    assert refused.value.stage == "readback"
    assert steps(calls)[-1] == "source-readback"
    assert attempt(tmp_path).is_dir()


@pytest.mark.parametrize(
    "answer",
    [
        ("status", (0, "?? packages/notes/notes/extra.py\n", "")),
        ("status", (0, "!! packages/notes/dist/\n", "")),
        (f"HEAD:{LOCAL.path}", (0, "4" * 40 + "\n", "")),
        ("HEAD^{commit}", (0, "e" * 40 + "\n", "")),
    ],
)
@pytest.mark.asyncio
async def test_a_source_that_moved_before_the_install_commit_is_never_staged(
    tmp_path, monkeypatch, answer
):
    key, reply = answer

    def moved(args):
        step = git(args) or []
        if step[:1] == ["status"] and step[-1] == f":(literal){LOCAL.path}" and key == "status":
            return reply
        if step[:2] == ["rev-parse", "--verify"] and step[-1] == key:
            return reply
        return None

    calls, command = local_product(tmp_path, moved)
    with pytest.raises(InstallExecutionError, match="source_changed") as refused:
        await execute(tmp_path, command, monkeypatch)
    assert refused.value.stage == "commit"
    assert not {"add", "commit", "push"} & set(steps(calls))


@pytest.mark.parametrize(
    "lost",
    [
        lambda command: (
            command.verification is not None and command.verification.mode == "readback"
        ),
        lambda command: command.action == "publish",
    ],
    ids=["readback", "publish"],
)
@pytest.mark.asyncio
async def test_a_lost_fence_stops_and_retains_the_attempt(tmp_path, monkeypatch, lost):
    calls, command = local_product(tmp_path)
    with pytest.raises(InstallExecutionError, match="lease_lost"):
        await execute(tmp_path, command, monkeypatch, Fence(refuse=lost))
    assert attempt(tmp_path).is_dir()
    assert not any((git(args) or [None])[:2] == ["worktree", "remove"] for args, _ in calls)
