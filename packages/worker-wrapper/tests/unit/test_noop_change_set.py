"""The scripted developer: change-set parsing, apply, hooks and step ordering.

These tests execute the runner's embedded script instead of matching its source text.
The two paths must treat the product's hooks in opposite ways, so both are also exercised
against a *real* local git repository with the product's hooks installed — the
fake-subprocess tests below cannot tell the difference, which is how the first submission
shipped a fallback that ran the product's hooks.
"""

import json
from pathlib import Path
import subprocess
import textwrap
from types import SimpleNamespace

import pytest
from worker_wrapper.injected_paths import EXCLUDE_LINES, offending_paths
from worker_wrapper.runners.noop import NoopRunner

SENTINEL = "codegen-change-set v1"


def load_script():
    """Exec the runner's script into its own namespace without running main()."""
    script = NoopRunner().build_command(prompt="ignored")[2]
    namespace = {"__name__": "noop_script"}
    exec(script, namespace)  # noqa: S102
    return namespace


def change_set(body: str, sentinel: str = SENTINEL) -> str:
    fence = "```codegen-change-set\n"
    if sentinel:
        fence += sentinel + "\n"
    return "# Task\n\nSome prose.\n\n" + fence + body + "```\n"


def capture_results(namespace) -> list:
    """Replace the script's urlopen with a recorder; no HTTP in a unit test."""
    payloads: list = []

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

    def urlopen(request, timeout):
        payloads.append(json.loads(request.data))
        return Response()

    namespace["urlopen"] = urlopen
    return payloads


class FakeGit:
    """Records every subprocess call and answers the ones the script inspects."""

    def __init__(self, failures=None):
        self.calls = []
        self.failures = failures or {}

    def __call__(self, args, **kwargs):
        args = tuple(args)
        self.calls.append(args)
        key = " ".join(args)
        if key in self.failures:
            code, stderr = self.failures[key]
            return SimpleNamespace(returncode=code, stdout="", stderr=stderr)
        core = ("git", *args[3:]) if args[:2] == ("git", "-c") else args
        if core[:3] == ("git", "rev-parse", "--abbrev-ref"):
            return SimpleNamespace(returncode=0, stdout="feature/scripted\n", stderr="")
        if core[:3] == ("git", "rev-parse", "--git-path"):
            return SimpleNamespace(returncode=0, stdout=".git/info/exclude\n", stderr="")
        if core[:2] == ("git", "config"):
            return SimpleNamespace(returncode=0, stdout=".githooks\n", stderr="")
        if core[:2] == ("git", "rev-parse"):
            return SimpleNamespace(returncode=0, stdout="abc1234\n", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")


def run_main(namespace, monkeypatch, workspace, failures=None):
    git = FakeGit(failures)
    monkeypatch.setattr(subprocess, "run", git)
    payloads = capture_results(namespace)
    exit_code = namespace["main"](str(workspace))
    return exit_code, git.calls, payloads


class TestChangeSetParse:
    def test_parses_create_replace_and_append(self):
        namespace = load_script()
        operations = namespace["parse_change_set"](
            change_set(
                textwrap.dedent(
                    """\
                    @@ create pkg/new.py
                    print("hi")
                    @@ replace README.md
                    # Product
                    @@ append .env.example
                    PING=1
                    """
                )
            )
        )

        assert operations == [
            ("create", "pkg/new.py", 'print("hi")\n'),
            ("replace", "README.md", "# Product\n"),
            ("append", ".env.example", "PING=1\n"),
        ]

    def test_directive_without_content_means_empty_file(self):
        namespace = load_script()

        assert namespace["parse_change_set"](change_set("@@ create pkg/__init__.py\n")) == [
            ("create", "pkg/__init__.py", "")
        ]

    def test_absent_block_returns_none(self):
        namespace = load_script()

        assert namespace["parse_change_set"]("# Task\n\nNo change set here.\n") is None
        assert namespace["parse_change_set"](None) is None

    def test_prose_that_only_names_the_marker_is_not_a_block(self):
        """Card 1301 quotes acceptance criteria verbatim, marker text included."""
        namespace = load_script()
        quoted = (
            "# Task\n\n"
            "1. Read a change set from a fenced block with an explicit marker\n"
            "   (e.g. ```codegen-change-set ... ```) holding a machine format.\n"
        )

        assert namespace["parse_change_set"](quoted) is None

    @pytest.mark.parametrize(
        "text",
        [
            "```codegen-change-set\ncodegen-change-set v1\n@@ create a.py\nbody\n",
            "```codegen-change-set\ncodegen-change-set v1\n```\n",
            "```codegen-change-set\ncodegen-change-set v1\n@@ destroy a.py\n```\n",
            "```codegen-change-set\ncodegen-change-set v1\n@@ create\n```\n",
            "```codegen-change-set\ncodegen-change-set v1\nstray\n@@ create a.py\n```\n",
        ],
        ids=["unclosed", "empty", "unknown-op", "no-path", "content-first"],
    )
    def test_malformed_block_is_refused(self, text):
        namespace = load_script()

        with pytest.raises(namespace["MalformedChangeSet"]):
            namespace["parse_change_set"](text)

    @pytest.mark.parametrize(
        "sentinel",
        ["", "codegen-change-set v2", "@@ create a.py"],
        ids=["missing", "wrong-version", "straight-to-directive"],
    )
    def test_block_without_the_version_sentinel_is_refused(self, sentinel):
        """A quoted example that lacks the sentinel is never executed as a change set."""
        namespace = load_script()
        text = change_set("@@ create a.py\nbody\n", sentinel=sentinel)

        with pytest.raises(namespace["MalformedChangeSet"]) as raised:
            namespace["parse_change_set"](text)

        assert "sentinel" in str(raised.value)

    def test_two_candidate_blocks_are_refused_rather_than_guessed(self):
        """A card that documents the format plus a real change set: refuse, don't pick."""
        namespace = load_script()
        documented = change_set("@@ create doc/example.py\nexample\n")
        real = change_set("@@ create pkg/real.py\nreal\n")

        with pytest.raises(namespace["MalformedChangeSet"]) as raised:
            namespace["parse_change_set"](documented + real)

        assert "2 change-set blocks" in str(raised.value)

    @pytest.mark.parametrize(
        "path",
        ["/etc/passwd", "../outside.py", "pkg/../../outside.py", "~/.ssh/authorized_keys"],
    )
    def test_path_escaping_the_workspace_is_refused(self, path):
        namespace = load_script()

        with pytest.raises(namespace["PathEscapesWorkspace"]):
            namespace["parse_change_set"](change_set(f"@@ create {path}\nbody\n"))


class TestChangeSetApply:
    def test_applies_every_operation_kind(self, tmp_path):
        namespace = load_script()
        (tmp_path / "README.md").write_text("old\n")
        (tmp_path / "log.txt").write_text("first\n")

        namespace["apply_change_set"](
            [
                ("create", "pkg/new.py", "body\n"),
                ("replace", "README.md", "# Product\n"),
                ("append", "log.txt", "second\n"),
            ],
            str(tmp_path),
        )

        assert (tmp_path / "pkg/new.py").read_text() == "body\n"
        assert (tmp_path / "README.md").read_text() == "# Product\n"
        assert (tmp_path / "log.txt").read_text() == "first\nsecond\n"

    def test_create_refuses_an_existing_file(self, tmp_path):
        namespace = load_script()
        (tmp_path / "taken.py").write_text("mine\n")

        with pytest.raises(namespace["ChangeSetTargetExists"]):
            namespace["apply_change_set"]([("create", "taken.py", "new\n")], str(tmp_path))

        assert (tmp_path / "taken.py").read_text() == "mine\n"

    @pytest.mark.parametrize("op", ["replace", "append"])
    def test_replace_and_append_require_an_existing_file(self, tmp_path, op):
        namespace = load_script()

        with pytest.raises(namespace["ChangeSetTargetMissing"]):
            namespace["apply_change_set"]([(op, "missing.py", "body\n")], str(tmp_path))

    def test_path_escape_writes_nothing(self, tmp_path, monkeypatch):
        namespace = load_script()
        (tmp_path / "TASK.md").write_text(
            change_set("@@ create pkg/ok.py\nbody\n@@ replace ../escape.py\nevil\n")
        )

        exit_code, calls, payloads = run_main(namespace, monkeypatch, tmp_path)

        assert exit_code == 1
        assert payloads[0]["error_class"] == "PathEscapesWorkspace"
        assert payloads[0]["step"] == "change_set"
        assert not (tmp_path / "pkg").exists()
        assert not (tmp_path.parent / "escape.py").exists()
        assert calls == []


class TestScriptedRun:
    def _task(self, tmp_path):
        (tmp_path / "README.md").write_text("old\n")
        (tmp_path / "TASK.md").write_text(
            change_set("@@ create pkg/new.py\nbody\n@@ replace README.md\n# Product\n")
        )

    def test_applies_change_set_then_setup_then_commit_then_push(self, tmp_path, monkeypatch):
        namespace = load_script()
        self._task(tmp_path)

        exit_code, calls, payloads = run_main(namespace, monkeypatch, tmp_path)

        assert exit_code == 0
        assert (tmp_path / "pkg/new.py").read_text() == "body\n"
        assert (tmp_path / "README.md").read_text() == "# Product\n"

        setup = calls.index(("make", "setup"))
        stage = calls.index(("git", "add", "-A"))
        commit = next(i for i, call in enumerate(calls) if call[:2] == ("git", "commit"))
        push = next(i for i, call in enumerate(calls) if call[:2] == ("git", "push"))
        assert setup < stage < commit < push
        assert calls[push] == ("git", "push", "origin", "feature/scripted")
        assert payloads[0]["success"] is True
        assert payloads[0]["commit"] == "abc1234"

    def test_hooks_are_active_for_the_scripted_commit(self, tmp_path, monkeypatch):
        namespace = load_script()
        self._task(tmp_path)

        _, calls, _ = run_main(namespace, monkeypatch, tmp_path)

        assert not any("/dev/null" in arg for call in calls for arg in call)
        assert not any(call[:3] == ("git", "config", "core.hooksPath") for call in calls), (
            "the runner must never write the product's hooksPath"
        )
        assert "--no-verify" not in [arg for call in calls for arg in call]

    def test_hooks_path_guard_reads_the_local_config(self, tmp_path, monkeypatch):
        """A global or system core.hooksPath must not be able to satisfy the guard."""
        namespace = load_script()
        self._task(tmp_path)

        _, calls, _ = run_main(namespace, monkeypatch, tmp_path)

        assert ("git", "config", "--local", "--get", "core.hooksPath") in calls

    def test_a_hooks_path_that_setup_did_not_configure_fails_the_run(self, tmp_path, monkeypatch):
        namespace = load_script()
        self._task(tmp_path)
        failures = {"git config --local --get core.hooksPath": (1, "")}

        exit_code, calls, payloads = run_main(namespace, monkeypatch, tmp_path, failures)

        assert exit_code == 1
        assert payloads[0]["step"] == "hooks_path"
        assert payloads[0]["error_class"] == "HooksPathNotConfigured"
        assert not any(call[:2] == ("git", "commit") for call in calls)

    def test_setup_failure_is_reported_and_stops_the_run(self, tmp_path, monkeypatch):
        namespace = load_script()
        self._task(tmp_path)
        failures = {"make setup": (2, "make: *** [setup] Error 2\n")}

        exit_code, calls, payloads = run_main(namespace, monkeypatch, tmp_path, failures)

        assert exit_code == 2
        payload = payloads[0]
        assert payload["success"] is False
        assert payload["step"] == "setup"
        assert payload["error_class"] == "SetupFailed"
        assert payload["exit_code"] == 2
        assert "Error 2" in payload["stderr"]
        assert not any(call[:2] == ("git", "commit") for call in calls)

    def test_hook_failure_on_commit_is_reported_not_skipped(self, tmp_path, monkeypatch):
        namespace = load_script()
        self._task(tmp_path)
        failures = {
            "git commit -m feat: apply scripted change set": (1, "pre-commit: ruff check failed\n")
        }

        exit_code, calls, payloads = run_main(namespace, monkeypatch, tmp_path, failures)

        assert exit_code == 1
        assert payloads[0]["step"] == "commit"
        assert "ruff check failed" in payloads[0]["stderr"]
        assert not any(call[:2] == ("git", "push") for call in calls)

    def test_reported_stderr_is_bounded_and_credential_free(self, tmp_path, monkeypatch):
        namespace = load_script()
        self._task(tmp_path)
        noise = "x" * 6000
        failures = {
            "git push origin feature/scripted": (
                128,
                noise + "fatal: https://token-like-value@example.invalid/repo.git\n",
            )
        }

        exit_code, _, payloads = run_main(namespace, monkeypatch, tmp_path, failures)

        assert exit_code == 128
        stderr = payloads[0]["stderr"]
        assert len(stderr) <= 4000
        assert "token-like-value" not in stderr
        assert "https://***@example.invalid/repo.git" in stderr


class TestFallbackRun:
    def test_no_task_file_keeps_the_empty_commit_and_push(self, tmp_path, monkeypatch):
        namespace = load_script()

        exit_code, calls, payloads = run_main(namespace, monkeypatch, tmp_path)

        assert exit_code == 0
        assert ("make", "setup") not in calls
        assert (
            "git",
            "-c",
            "core.hooksPath=/dev/null",
            "commit",
            "--allow-empty",
            "-m",
            "chore: noop marker for e2e test",
        ) in calls
        assert (
            "git",
            "-c",
            "core.hooksPath=/dev/null",
            "push",
            "origin",
            "feature/scripted",
        ) in calls
        assert payloads[0]["success"] is True
        assert payloads[0]["summary"] == "noop commit pushed to feature/scripted"

    def test_task_file_without_a_change_set_keeps_the_empty_commit(self, tmp_path, monkeypatch):
        namespace = load_script()
        (tmp_path / "TASK.md").write_text("# Task\n\nJust prose, no change set.\n")

        exit_code, calls, _ = run_main(namespace, monkeypatch, tmp_path)

        assert exit_code == 0
        assert ("make", "setup") not in calls
        assert any(call[3:5] == ("commit", "--allow-empty") for call in calls)

    def test_fallback_never_writes_the_workspace_hooks_config(self, tmp_path, monkeypatch):
        namespace = load_script()

        _, calls, _ = run_main(namespace, monkeypatch, tmp_path)

        assert not any(call[:3] == ("git", "config", "core.hooksPath") for call in calls)


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(  # noqa: S603
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, timeout=60, check=True
    )
    return result.stdout.strip()


def _scaffolded_workspace(
    tmp_path: Path,
    pre_commit: str,
    pre_push: str,
    *,
    enable_hooks: bool,
) -> tuple[Path, Path]:
    """A bare remote plus a workspace clone carrying the product's hooks.

    This is the shape the scaffolder leaves behind: `.githooks/` installed and, once
    `make setup` has run, `core.hooksPath` pointing at it in the workspace's own config
    (`services/scaffolder/src/scaffold.py`).
    """
    remote = tmp_path / "remote.git"
    remote.mkdir()
    _git(remote, "init", "--bare", "--initial-branch=main")

    seed = tmp_path / "seed"
    seed.mkdir()
    _git(seed, "init", "--initial-branch=main")
    _git(seed, "config", "user.email", "ai@codegen.local")
    _git(seed, "config", "user.name", "Codegen Bot")
    (seed / "README.md").write_text("# product\n")
    _git(seed, "add", ".")
    _git(seed, "commit", "-m", "first story")
    _git(seed, "remote", "add", "origin", str(remote))
    _git(seed, "push", "-u", "origin", "main")

    workspace = tmp_path / "workspace"
    _git(tmp_path, "clone", str(remote), str(workspace))
    _git(workspace, "config", "user.email", "ai@codegen.local")
    _git(workspace, "config", "user.name", "Codegen Bot")
    hooks = workspace / ".githooks"
    hooks.mkdir()
    (hooks / "pre-commit").write_text(pre_commit)
    (hooks / "pre-commit").chmod(0o755)
    (hooks / "pre-push").write_text(pre_push)
    (hooks / "pre-push").chmod(0o755)
    if enable_hooks:
        _git(workspace, "config", "core.hooksPath", ".githooks")
    return remote, workspace


_FATAL_PRE_COMMIT = '#!/bin/sh\ntouch "$(git rev-parse --show-toplevel)/pre-commit-ran"\nexit 1\n'
_FATAL_PRE_PUSH = '#!/bin/sh\ntouch "$(git rev-parse --show-toplevel)/pre-push-ran"\nexit 1\n'


class TestFallbackAgainstRealProductHooks:
    """AC4 in the shape the live noop suites actually meet it.

    The developer workspace is the scaffolded product directory, and
    `services/scaffolder/src/scaffold.py` leaves `core.hooksPath=.githooks` in its own
    config. So the fallback's "empty commit" runs in a repository whose hooks are armed,
    and asserting its argv proves nothing about the config that argv runs against.
    """

    def test_the_fixture_hook_really_fires_without_an_override(self, tmp_path):
        """Guards the guard: without the override this workspace's hook takes over."""
        _, workspace = _scaffolded_workspace(
            tmp_path, _FATAL_PRE_COMMIT, _FATAL_PRE_PUSH, enable_hooks=True
        )

        result = subprocess.run(  # noqa: S603
            ["git", "commit", "--allow-empty", "-m", "unguarded"],
            cwd=str(workspace),
            capture_output=True,
            text=True,
            timeout=60,
        )

        assert result.returncode != 0
        assert (workspace / "pre-commit-ran").exists()

    def test_empty_commit_and_push_run_with_the_products_hooks_disabled(self, tmp_path):
        namespace = load_script()
        remote, workspace = _scaffolded_workspace(
            tmp_path, _FATAL_PRE_COMMIT, _FATAL_PRE_PUSH, enable_hooks=True
        )
        payloads = capture_results(namespace)

        exit_code = namespace["main"](str(workspace))

        assert exit_code == 0, payloads
        assert not (workspace / "pre-commit-ran").exists()
        assert not (workspace / "pre-push-ran").exists()
        assert payloads[0]["success"] is True
        assert _git(remote, "log", "-1", "--format=%s", "main") == (
            "chore: noop marker for e2e test"
        )
        # Per command only: the product's own config is left exactly as it was, so the
        # scripted path in the same workspace still gets its hooks.
        assert _git(workspace, "config", "--local", "--get", "core.hooksPath") == ".githooks"


class TestScriptedRunAgainstRealProductHooks:
    """AC2 plus the injected-file repair, against a real hook that runs `git add -A`."""

    def _workspace(self, tmp_path: Path) -> tuple[Path, Path]:
        # The kit's pre-commit runs `make format` and then `git add -A`; reduced to the
        # effect that matters here, it stages everything the worker left in the tree.
        pre_commit = '#!/bin/sh\ntouch "$(git rev-parse --git-dir)/pre-commit-ran"\ngit add -A\n'
        pre_push = '#!/bin/sh\ntouch "$(git rev-parse --git-dir)/pre-push-ran"\n'
        remote, workspace = _scaffolded_workspace(
            tmp_path, pre_commit, pre_push, enable_hooks=False
        )
        # The kit's `make setup` ends by enabling the hooks; that is the step the runner
        # has to run rather than simulate.
        (workspace / "Makefile").write_text("setup:\n\t@git config core.hooksPath .githooks\n")
        # Injected by the orchestrator for AgentType.NOOP, not the product's content.
        # One of every kind in the set: the two instruction files, the turn's own
        # documents, the agent's notes, the venv sentinel and the story archive.
        for name in ("CLAUDE.md", "WORKER_INSTRUCTIONS.md", "PROGRESS.md", "REPORT.md"):
            (workspace / name).write_text("# orchestrator internal\n")
        (workspace / ".venv_paths_fixed").write_text("")
        (workspace / ".story" / "old_tasks").mkdir(parents=True)
        (workspace / ".story" / "STORY.md").write_text("# story\n")
        (workspace / "TASK.md").write_text(change_set("@@ create pkg/new.py\nbody\n"))
        return remote, workspace

    def test_setup_enables_the_hooks_and_the_commit_runs_them(self, tmp_path):
        namespace = load_script()
        remote, workspace = self._workspace(tmp_path)
        payloads = capture_results(namespace)

        exit_code = namespace["main"](str(workspace))

        assert exit_code == 0, payloads
        assert payloads[0]["success"] is True
        assert (workspace / ".git/pre-commit-ran").exists(), "the product's hook must run"
        assert (workspace / ".git/pre-push-ran").exists()
        assert _git(workspace, "config", "--local", "--get", "core.hooksPath") == ".githooks"
        assert "pkg/new.py" in _git(remote, "show", "--name-only", "--format=", "HEAD")

    def test_the_managers_injected_files_are_never_committed(self, tmp_path):
        namespace = load_script()
        remote, workspace = self._workspace(tmp_path)
        capture_results(namespace)

        assert namespace["main"](str(workspace)) == 0

        committed = _git(remote, "show", "--name-only", "--format=", "HEAD").split()
        assert "pkg/new.py" in committed
        assert offending_paths(committed) == [], "no injected path may reach the product"
        assert (workspace / "CLAUDE.md").exists(), "excluded, not deleted"
        exclude = (workspace / ".git/info/exclude").read_text()
        assert all(line in exclude for line in EXCLUDE_LINES)

    def test_the_excluded_set_is_the_packages_one(self, tmp_path):
        """The script carries the set as data; it does not keep a second copy of it."""
        namespace = load_script()

        assert namespace["WORKER_INTERNAL_FILES"] == list(EXCLUDE_LINES)

    def test_the_exclude_rule_is_written_once(self, tmp_path):
        namespace = load_script()
        _, workspace = self._workspace(tmp_path)

        namespace["exclude_worker_internal_files"](str(workspace))
        namespace["exclude_worker_internal_files"](str(workspace))

        assert (workspace / ".git/info/exclude").read_text().count("/CLAUDE.md") == 1
