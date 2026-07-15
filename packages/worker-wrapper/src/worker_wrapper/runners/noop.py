from dataclasses import dataclass

from .base import AgentRunner


@dataclass
class NoopRunner(AgentRunner):
    """Runner for E2E testing — empty commit + push, no LLM."""

    def build_command(self, prompt: str) -> list[str]:
        script = """\
import json
import subprocess
from urllib.request import Request, urlopen


def run(*args):
    return subprocess.run(args, capture_output=True, text=True, timeout=60)


branch_result = run("git", "rev-parse", "--abbrev-ref", "HEAD")
branch = branch_result.stdout.strip()
config = run("git", "config", "core.hooksPath", "/dev/null")
commit = (
    run("git", "commit", "--allow-empty", "-m", "chore: noop marker for e2e test")
    if branch_result.returncode == 0 and branch != "HEAD" and config.returncode == 0
    else None
)
push = (
    run("git", "push", "origin", branch)
    if commit is not None and commit.returncode == 0
    else None
)
if push is not None and push.returncode == 0:
    sha = run("git", "rev-parse", "HEAD").stdout.strip()
    payload = {"success": True, "commit": sha, "summary": f"noop commit pushed to {branch}"}
    exit_code = 0
else:
    failed = push or commit or config or branch_result
    payload = {
        "success": False,
        "reason": "noop git command failed; inspect worker logs for command diagnostics",
        "error_class": "GitCommandFailed",
        "exit_code": failed.returncode or 1,
    }
    exit_code = failed.returncode or 1

request = Request(
    "http://127.0.0.1:9090/result",
    data=json.dumps(payload).encode(),
    headers={"Content-Type": "application/json"},
    method="POST",
)
with urlopen(request, timeout=10) as response:
    if response.status != 200:
        raise RuntimeError(f"result reporting failed: HTTP {response.status}")
raise SystemExit(exit_code)
"""
        return [
            "python3",
            "-c",
            script,
        ]
