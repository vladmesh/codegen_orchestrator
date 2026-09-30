"""Fake `codex` and `claude` executables on a temporary PATH.

Each fake is a real executable the channel finds with `shutil.which` and runs as
a subprocess, so what is under test is the channel's own command line, stdin,
environment, working directory and result parsing. A fake answers from a
scripted list of responses (the last one repeats) and records every invocation:
argv, environment, stdin, working directory and its contents.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import stat
import sys
from typing import Any

_SCRIPT = """#!{python}
import json, os, sys, time
from pathlib import Path

base = Path({base!r})
spec = json.loads((base / "spec.json").read_text())
if sys.argv[1:] == ["--version"]:
    with (base / "version_calls.jsonl").open("a") as log:
        log.write(json.dumps({{"env": dict(os.environ), "cwd": os.getcwd()}}) + "\\n")
    print(spec.get("version", "fake-cli 0.0.0"))
    sys.exit(0)
calls = base / "calls.jsonl"
index = sum(1 for _ in calls.open()) if calls.exists() else 0
stdin = sys.stdin.read()
with calls.open("a") as log:
    log.write(json.dumps({{
        "started": time.time(),
        "argv": sys.argv[1:],
        "env": dict(os.environ),
        "stdin": stdin,
        "cwd": os.getcwd(),
        "cwd_listing": sorted(os.listdir(".")),
        "home_writable": os.access(os.environ.get("HOME", "/nonexistent"), os.W_OK),
    }}) + "\\n")
responses = spec["responses"]
response = responses[min(index, len(responses) - 1)]
time.sleep(response.get("sleep", 0))
if response.get("stderr"):
    sys.stderr.write(response["stderr"])
answer = response.get("answer")
raw = response.get("raw")
text = raw if raw is not None else (json.dumps(answer) if answer is not None else None)
if spec["kind"] == "codex":
    argv = sys.argv[1:]
    if text is not None and "--output-last-message" in argv:
        Path(argv[argv.index("--output-last-message") + 1]).write_text(text)
else:
    if response.get("envelope") is not None:
        print(json.dumps(response["envelope"]))
    elif text is not None:
        if raw is not None:
            print(raw)
        else:
            print(json.dumps({{"type": "result", "is_error": False, "structured_output": answer}}))
sys.exit(response.get("exit", 0))
"""


def turn(content: str = "", tool_calls: list[tuple[str, Any]] | None = None) -> dict:
    """A schema-valid answer; tool arguments are JSON-encoded unless given as a string."""
    return {
        "content": content,
        "tool_calls": [
            {
                "name": name,
                "arguments_json": arguments
                if isinstance(arguments, str)
                else json.dumps(arguments),
            }
            for name, arguments in (tool_calls or [])
        ],
    }


@dataclass
class FakeCli:
    kind: str
    base: Path

    def script(self, *responses: dict, version: str | None = None) -> FakeCli:
        spec: dict[str, Any] = {"kind": self.kind, "responses": list(responses)}
        if version is not None:
            spec["version"] = version
        (self.base / "spec.json").write_text(json.dumps(spec))
        return self

    @property
    def calls(self) -> list[dict]:
        return self._read("calls.jsonl")

    @property
    def version_calls(self) -> list[dict]:
        """`--version` invocations, which answer the version and are not model turns."""
        return self._read("version_calls.jsonl")

    def _read(self, name: str) -> list[dict]:
        path = self.base / name
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines()]


def install(bin_dir: Path, kind: str) -> FakeCli:
    """Put an executable named `kind` in `bin_dir`, answering plain content by default."""
    base = bin_dir.parent / f"{kind}-fake"
    base.mkdir(exist_ok=True)
    executable = bin_dir / kind
    executable.write_text(_SCRIPT.format(python=sys.executable, base=str(base)))
    executable.chmod(executable.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return FakeCli(kind, base).script({"answer": turn(f"answer from {kind}")})
