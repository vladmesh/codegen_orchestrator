"""The scripted developer: change-set parsing, apply, and step ordering.

These tests execute the runner's embedded script instead of matching its source text,
so the parse, the apply and the failure payload are exercised as real behaviour.
"""

import json
import subprocess
import textwrap
from types import SimpleNamespace

import pytest
from worker_wrapper.runners.noop import NoopRunner


def load_script():
    """Exec the runner's script into its own namespace without running main()."""
    script = NoopRunner().build_command(prompt="ignored")[2]
    namespace = {"__name__": "noop_script"}
    exec(script, namespace)  # noqa: S102
    return namespace


def change_set(body: str) -> str:
    return "# Task\n\nSome prose.\n\n```codegen-change-set\n" + body + "```\n"


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
        if args[:3] == ("git", "rev-parse", "--abbrev-ref"):
            return SimpleNamespace(returncode=0, stdout="feature/scripted\n", stderr="")
        if args[:3] == ("git", "config", "--get"):
            return SimpleNamespace(returncode=0, stdout=".githooks\n", stderr="")
        if args[:2] == ("git", "rev-parse"):
            return SimpleNamespace(returncode=0, stdout="abc1234\n", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")


def run_main(namespace, monkeypatch, workspace, failures=None):
    git = FakeGit(failures)
    monkeypatch.setattr(subprocess, "run", git)
    payloads = []

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

    @pytest.mark.parametrize(
        "text",
        [
            "```codegen-change-set\n@@ create a.py\nbody\n",
            "```codegen-change-set\n```\n",
            "```codegen-change-set\n@@ destroy a.py\n```\n",
            "```codegen-change-set\n@@ create\n```\n",
            "```codegen-change-set\nstray content\n@@ create a.py\n```\n",
        ],
        ids=["unclosed", "empty", "unknown-op", "no-path", "content-first"],
    )
    def test_malformed_block_is_refused(self, text):
        namespace = load_script()

        with pytest.raises(namespace["MalformedChangeSet"]):
            namespace["parse_change_set"](text)

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

    def test_hooks_are_active_for_the_commit(self, tmp_path, monkeypatch):
        namespace = load_script()
        self._task(tmp_path)

        _, calls, _ = run_main(namespace, monkeypatch, tmp_path)

        assert not any("/dev/null" in arg for call in calls for arg in call)
        assert not any(call[:3] == ("git", "config", "core.hooksPath") for call in calls), (
            "the runner must not override the product's hooksPath"
        )
        assert ("git", "config", "--get", "core.hooksPath") in calls
        assert "--no-verify" not in [arg for call in calls for arg in call]

    def test_a_hooks_path_that_setup_did_not_configure_fails_the_run(self, tmp_path, monkeypatch):
        namespace = load_script()
        self._task(tmp_path)
        failures = {"git config --get core.hooksPath": (1, "")}

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
            "commit",
            "--allow-empty",
            "-m",
            "chore: noop marker for e2e test",
        ) in calls
        assert ("git", "push", "origin", "feature/scripted") in calls
        assert payloads[0]["success"] is True
        assert payloads[0]["summary"] == "noop commit pushed to feature/scripted"

    def test_task_file_without_a_change_set_keeps_the_empty_commit(self, tmp_path, monkeypatch):
        namespace = load_script()
        (tmp_path / "TASK.md").write_text("# Task\n\nJust prose, no change set.\n")

        exit_code, calls, _ = run_main(namespace, monkeypatch, tmp_path)

        assert exit_code == 0
        assert ("make", "setup") not in calls
        assert any(call[:3] == ("git", "commit", "--allow-empty") for call in calls)
