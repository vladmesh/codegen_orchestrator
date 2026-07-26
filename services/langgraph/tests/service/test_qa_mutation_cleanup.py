"""QA mutation journal integration tests against a real HTTP application."""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import sys
from threading import Thread

import pytest

from shared.contracts.dto.run_result import QABlocker, QABlockerCategory, QARunResult
from shared.contracts.queues.qa import QAOutcome
from src.consumers._qa_runner import QA_MUTATION_TOOL_SOURCE


@pytest.fixture(autouse=True)
def _clean_redis():
    """This self-contained application exercise does not use Redis."""
    yield


@pytest.fixture
def empty_users_application():
    """A deployed-app stand-in whose users table starts empty for every run."""
    users: set[str] = set()
    cleanup_fails = {"value": False}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            if self.path != "/users":
                self.send_error(404)
                return
            users.add(json.loads(self.rfile.read(int(self.headers["Content-Length"])))["id"])
            self.send_response(201)
            self.end_headers()

        def do_DELETE(self):  # noqa: N802
            user_id = self.path.removeprefix("/users/")
            if cleanup_fails["value"]:
                self.send_error(405)
                return
            users.discard(user_id)
            self.send_response(204)
            self.end_headers()

        def log_message(self, format, *args):  # noqa: A002
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", users, cleanup_fails
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


def _run_tool(tool: Path, journal: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = os.environ | {"QA_MUTATION_JOURNAL": str(journal)}
    return subprocess.run(  # noqa: S603
        [sys.executable, str(tool), *args],
        check=True,
        text=True,
        capture_output=True,
        env=env,
    )


@pytest.mark.parametrize("cleanup_fails", [False, True])
def test_qa_run_cleanup_leaves_empty_users_or_a_non_passing_residue(
    tmp_path: Path, empty_users_application, cleanup_fails: bool
):
    """Drive a journaled QA mutation against an empty app for both terminal paths."""
    base_url, users, failure_switch = empty_users_application
    failure_switch["value"] = cleanup_fails
    tool = tmp_path / "qa-state-mutation.py"
    journal = tmp_path / "journal.json"
    tool.write_text(QA_MUTATION_TOOL_SOURCE)

    _run_tool(
        tool,
        journal,
        "request",
        "POST",
        f"{base_url}/users",
        '{"id":"8202532144"}',
        "DELETE",
        f"{base_url}/users/8202532144",
        "",
    )
    journal_entries = json.loads(_run_tool(tool, journal, "cleanup").stdout)
    state_changes = [
        {
            "resource": entry["resource"],
            "operation": entry["operation"],
            "cleanup": entry["cleanup"],
        }
        for entry in journal_entries
    ]

    if not cleanup_fails:
        assert users == set()
        assert QARunResult(qa_outcome=QAOutcome.PASSED, state_changes=state_changes)
    else:
        assert users == {"8202532144"}
        assert state_changes[0]["cleanup"]["succeeded"] is False
        with pytest.raises(ValueError, match="uncleaned state change"):
            QARunResult(qa_outcome=QAOutcome.PASSED, state_changes=state_changes)
        result = QARunResult(
            qa_outcome=QAOutcome.BLOCKED,
            blocker=QABlocker(
                category=QABlockerCategory.QA_CLEANUP_FAILED,
                attempted="cleanup QA mutation journal",
                sent="DELETE /users/8202532144",
                received="HTTP 405",
            ),
            state_changes=state_changes,
        )
        assert result.qa_outcome is QAOutcome.BLOCKED
