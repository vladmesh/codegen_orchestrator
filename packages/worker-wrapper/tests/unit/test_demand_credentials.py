"""Native credential protocol and gh use transient, per-command auth."""

import io
import os
from pathlib import Path
import sys
from unittest.mock import MagicMock

import pytest
from worker_wrapper import credentials

from shared.tests.worker_credential_fixture import WorkerCredentialFixture


@pytest.mark.parametrize("operation", ["store", "erase"])
def test_helper_persists_nothing(operation, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(credentials, "request_token", MagicMock(side_effect=AssertionError("mint")))
    output = io.StringIO()
    assert credentials.git_credential(operation, io.StringIO("password=sentinel\n\n"), output) == 0
    assert output.getvalue() == ""
    assert list(tmp_path.iterdir()) == []


def test_get_reacquires_and_checks_native_path(monkeypatch):
    mint = MagicMock(side_effect=["first", "after-expiry"])
    monkeypatch.setattr(credentials, "request_token", mint)
    for token in ("first", "after-expiry"):
        output = io.StringIO()
        assert (
            credentials.git_credential(
                "get", io.StringIO("protocol=https\nhost=github.com\npath=org/repo.git\n\n"), output
            )
            == 0
        )
        assert output.getvalue() == f"username=x-access-token\npassword={token}\n\n"
    assert mint.call_count == 2
    with pytest.raises(ValueError):
        credentials.git_credential(
            "get", io.StringIO("protocol=http\nhost=evil\n\n"), io.StringIO()
        )


def test_gh_reacquires_without_token_in_argv(monkeypatch):
    monkeypatch.setattr(credentials, "repository_from_origin", lambda: "org/repo")
    monkeypatch.setattr(
        credentials, "request_token", MagicMock(side_effect=["first", "after-expiry"])
    )
    run = MagicMock(return_value=MagicMock(returncode=0))
    monkeypatch.setattr(credentials.subprocess, "run", run)
    for token in ("first", "after-expiry"):
        assert credentials.gh_command(["api", "repos/org/repo"]) == 0
        assert run.call_args.kwargs["env"]["GH_TOKEN"] == token
        assert token not in str(run.call_args.args)
    with pytest.raises(ValueError):
        credentials.gh_command(["auth", "login"])


@pytest.mark.parametrize(
    "args",
    [
        ["issue", "list", "--label", "auth"],
        ["--repo", "auth", "issue", "list"],
        ["-Rauth", "issue", "list", "--label", "auth"],
    ],
)
def test_gh_non_auth_command_accepts_auth_argument(args, monkeypatch):
    monkeypatch.setattr(credentials, "repository_from_origin", lambda: "org/repo")
    mint = MagicMock(return_value="synthetic-command-token")
    monkeypatch.setattr(credentials, "request_token", mint)
    run = MagicMock(return_value=MagicMock(returncode=0))
    monkeypatch.setattr(credentials.subprocess, "run", run)
    assert credentials.gh_command(args) == 0
    assert run.call_args.args[0] == ["/usr/lib/codegen/gh", *args]
    assert run.call_args.kwargs["env"]["GH_TOKEN"] == mint.return_value
    assert "synthetic-command-token" not in str(run.call_args.args)
    mint.assert_called_once_with("org/repo")


@pytest.mark.parametrize("prefix", [[], ["--help"], ["-h"], ["--version"], ["--repo", "org/repo"]])
@pytest.mark.parametrize("subcommand", ["login", "setup-git", "token", "status"])
def test_gh_auth_command_refuses_before_credentials(prefix, subcommand, monkeypatch):
    mint = MagicMock(side_effect=AssertionError("mint"))
    run = MagicMock(side_effect=AssertionError("native gh"))
    monkeypatch.setattr(credentials, "request_token", mint)
    monkeypatch.setattr(credentials.subprocess, "run", run)
    with pytest.raises(ValueError, match="persistent gh"):
        credentials.gh_command([*prefix, "auth", subcommand])
    mint.assert_not_called()
    run.assert_not_called()


@pytest.mark.parametrize("args", [["api", "repos/org/repo"], ["issue", "list", "--label", "auth"]])
def test_native_gh_child_uses_new_auth_for_later_command(args, tmp_path, monkeypatch, capfd):
    native = tmp_path / "native-gh"
    native.write_text(
        f"#!{sys.executable} -I\nimport os, sys\n"
        "assert os.environ['GH_TOKEN']\nassert 'GITHUB_TOKEN' not in os.environ\n"
        "assert all(os.environ['GH_TOKEN'] not in arg for arg in sys.argv)\n"
        "sys.stdout.write('authenticated\\n')\n"
    )
    native.chmod(0o755)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(credentials, "repository_from_origin", lambda: "org/repo")
    original_run = credentials.subprocess.run
    with WorkerCredentialFixture(tmp_path) as broker:
        for key, value in broker.environment.items():
            monkeypatch.setenv(key, value)

        def run(args, **kwargs):
            assert args[0] == "/usr/lib/codegen/gh"
            assert kwargs["env"]["GH_TOKEN"] == broker.token
            assert broker.token not in str(args)
            return original_run([str(native), *args[1:]], **kwargs)

        monkeypatch.setattr(credentials.subprocess, "run", run)
        for token in ("synthetic-first", "synthetic-after-expiry"):
            broker.token = token
            assert credentials.gh_command(args) == 0
            output = capfd.readouterr()
            assert token not in output.out + output.err
            assert output.out == "authenticated\n"
        assert broker.requests == [{"repository": "org/repo"}] * 2
    assert not (Path(os.environ["HOME"]) / ".config/gh/hosts.yml").exists()


@pytest.mark.asyncio
async def test_auth_refusal_before_each_reused_turn_starts_no_agent(monkeypatch):
    from unittest.mock import AsyncMock

    from worker_wrapper.config import WorkerWrapperConfig
    from worker_wrapper.wrapper import WorkerWrapper

    broker = AsyncMock()
    wrapper = WorkerWrapper(
        WorkerWrapperConfig(
            worker_id="w", broker_url="http://broker", broker_token="x" * 43, agent_type="noop"
        ),
        broker_client=broker,
    )
    monkeypatch.setattr(wrapper, "_git_auth_preflight", lambda: False)
    wrapper.execute_agent = AsyncMock()
    wrapper._prepare_workspace = AsyncMock()
    for lease in ("first", "reused"):
        await wrapper._run_turn(lease, {"request_id": lease})
        result = broker.submit_output.await_args.args[1]
        assert result.execution.execution_phase.value == "pre_agent_refused"
        assert result.execution.infrastructure_refusal.value == "repository_auth_unavailable"
        assert result.cost_usd is None and result.input_tokens is None
    wrapper.execute_agent.assert_not_awaited()
    wrapper._prepare_workspace.assert_not_awaited()
