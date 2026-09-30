"""Does the installed CLI accept every flag its channel adapter passes?

The adapters (`cli_turn.py`) build their command lines for the CLI versions the
worker images pin. A flag a pinned CLI does not know fails every call of that
channel with a usage error, silently moving the traffic to the next channel.
This module names the flags an adapter's command line uses and checks them
against the CLI's own help output. The langgraph image build runs it against
the CLIs it just installed (`python -m src.llm.cli_contract`) and fails naming
any missing flag; the unit tests run it against help output captured from the
pinned versions.
"""

from __future__ import annotations

from collections.abc import Callable
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

from shared.contracts.dto.llm_channel import LLMChannel

from .cli_turn import ClaudeTurnModel, CliTurnChatModel, CodexTurnModel

#: The help output that lists the flags an adapter's command line uses.
HELP_COMMANDS: dict[LLMChannel, tuple[str, ...]] = {
    LLMChannel.CODEX: ("codex", "exec", "--help"),
    LLMChannel.CLAUDE: ("claude", "--help"),
}

#: How a help text says a positional `-` reads the prompt from stdin.
_STDIN_PROMPT = re.compile(r"`-` is used")

_MODELS: dict[LLMChannel, Callable[[], CliTurnChatModel]] = {
    LLMChannel.CODEX: lambda: CodexTurnModel(model="contract-model"),
    LLMChannel.CLAUDE: lambda: ClaudeTurnModel(model="contract-model"),
}


def adapter_command(channel: LLMChannel) -> list[str]:
    """The adapter's full command line, a model included, without the executable."""
    with tempfile.TemporaryDirectory() as root:
        command = _MODELS[channel]()._command(channel.value, Path(root), Path(root))
    return command[1:]


def adapter_flags(channel: LLMChannel) -> list[str]:
    """Every option the adapter passes, in command-line order."""
    return [argument for argument in adapter_command(channel) if re.match(r"-[-\w]", argument)]


def missing_flags(channel: LLMChannel, help_text: str) -> list[str]:
    """The adapter's options that `help_text` does not list; a stdin `-` counts as one."""
    missing = [
        flag
        for flag in adapter_flags(channel)
        if not re.search(rf"(?<![\w-]){re.escape(flag)}(?![\w-])", help_text)
    ]
    command = adapter_command(channel)
    if command[-1] == "-" and not _STDIN_PROMPT.search(help_text):
        missing.append("- (prompt on stdin)")
    return missing


def _help_text(channel: LLMChannel) -> str:
    binary, *arguments = HELP_COMMANDS[channel]
    executable = shutil.which(binary)
    if executable is None:
        raise SystemExit(f"{binary} is not on PATH")
    # A throwaway HOME: the CLIs write under their home even for --help.
    with tempfile.TemporaryDirectory() as home:
        result = subprocess.run(  # noqa: S603 - fixed argv of an installed CLI
            [executable, *arguments],
            capture_output=True,
            text=True,
            check=False,
            cwd=home,
            env={"PATH": os.environ.get("PATH", ""), "HOME": home},
            timeout=60,
        )
    if result.returncode != 0:
        raise SystemExit(f"{' '.join(HELP_COMMANDS[channel])} exited {result.returncode}")
    return result.stdout


def main() -> int:
    failures = []
    for channel in HELP_COMMANDS:
        missing = missing_flags(channel, _help_text(channel))
        if missing:
            failures.append(f"{channel.value}: {', '.join(missing)}")
    if failures:
        sys.stderr.write(
            "the installed CLIs do not accept flags the channel adapters pass: "
            + "; ".join(failures)
            + "\n"
        )
        return 1
    sys.stdout.write("every channel adapter flag is accepted by the installed CLIs\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
