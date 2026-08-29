"""``python -m shared`` is the canonical broad unit suite: the tree's own runner, same coverage."""

from __future__ import annotations

import os
from pathlib import Path
import runpy
import sys

import pytest

from shared import __main__ as entry

ROOT = Path(__file__).resolve().parents[2]


def test_runner_is_the_trees_own_script() -> None:
    assert entry.ROOT == ROOT
    assert entry.UNIT_SCRIPT == ROOT / "scripts" / "test-unit-local.sh"
    assert entry.UNIT_SCRIPT.is_file()


def test_command_forwards_caller_flags() -> None:
    assert entry.build_command(["--serial"]) == ["bash", str(entry.UNIT_SCRIPT), "--serial"]
    assert entry.build_command([]) == ["bash", str(entry.UNIT_SCRIPT)]


def test_env_puts_interpreter_dir_first_on_path(tmp_path: Path) -> None:
    venv_bin = tmp_path / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    interpreter = venv_bin / "python"
    interpreter.write_text("")
    env = entry.build_env({"PATH": f"/usr/bin{os.pathsep}{venv_bin}"}, str(interpreter))
    assert env["PATH"].split(os.pathsep) == [str(venv_bin), "/usr/bin"]
    assert env["VIRTUAL_ENV"] == str(tmp_path / ".venv")


def test_env_does_not_follow_the_interpreter_symlink(tmp_path: Path) -> None:
    toolchain = tmp_path / "toolchain" / "bin"
    toolchain.mkdir(parents=True)
    (toolchain / "python3.12").write_text("")
    venv_bin = tmp_path / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    (venv_bin / "python").symlink_to(toolchain / "python3.12")
    env = entry.build_env({"PATH": "/usr/bin"}, str(venv_bin / "python"))
    assert env["PATH"].split(os.pathsep)[0] == str(venv_bin)


def test_env_keeps_explicit_virtual_env() -> None:
    env = entry.build_env({"PATH": "", "VIRTUAL_ENV": "/elsewhere"}, sys.executable)
    assert env["VIRTUAL_ENV"] == "/elsewhere"
    assert env["PATH"].split(os.pathsep)[0] == str(Path(sys.executable).absolute().parent)


def test_main_reports_missing_runner(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(entry, "UNIT_SCRIPT", tmp_path / "absent.sh")
    assert entry.main([]) == 2


def test_main_returns_runner_exit_code(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, object] = {}

    class _Completed:
        returncode = 7

    def fake_run(command, *, cwd, env, check):  # noqa: ANN001
        seen.update(command=command, cwd=cwd, path=env["PATH"], check=check)
        return _Completed()

    monkeypatch.setattr(entry.subprocess, "run", fake_run)
    assert entry.main(["--serial"]) == 7
    assert seen["command"] == entry.build_command(["--serial"])
    assert seen["cwd"] == ROOT
    assert seen["check"] is False
    assert str(seen["path"]).split(os.pathsep)[0] == str(Path(sys.executable).absolute().parent)


def test_module_is_runnable_by_name(monkeypatch: pytest.MonkeyPatch) -> None:
    """``runpy`` is how ``check broad --module shared`` starts it; the guard must call main()."""
    calls: list[list[str]] = []

    class _Completed:
        returncode = 0

    def fake_run(command, **_):  # noqa: ANN001
        calls.append(command)
        return _Completed()

    monkeypatch.setattr(sys, "argv", ["shared", "--serial"])
    monkeypatch.setattr("subprocess.run", fake_run)
    monkeypatch.delitem(sys.modules, "shared.__main__", raising=False)
    with pytest.raises(SystemExit) as exit_info:
        runpy.run_module("shared", run_name="__main__", alter_sys=True)
    assert exit_info.value.code == 0
    assert calls == [entry.build_command(["--serial"])]
