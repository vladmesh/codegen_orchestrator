"""The channel adapters' flags against the pinned CLIs, and the image that installs them.

`tests/fixtures/cli_help/` holds the help output of exactly the versions the
worker images pin (`codex exec --help` of Codex CLI 0.144.6, `claude --help` of
Claude Code 2.1.278), captured from the released binaries. The image build runs
the same check against the CLIs it installed (`python -m src.llm.cli_contract`).
A pin bump without fresh help output fails here, because the fixtures are named
by version.
"""

from __future__ import annotations

from pathlib import Path
import re

import pytest

from shared.contracts.dto.llm_channel import LLMChannel
from src.llm.cli_contract import adapter_command, adapter_flags, missing_flags

REPO = Path(__file__).resolve().parents[5]
HELP = Path(__file__).resolve().parents[2] / "fixtures" / "cli_help"
LANGGRAPH_DOCKERFILE = REPO / "services/langgraph/Dockerfile"
PINS = {
    LLMChannel.CODEX: (
        REPO / "services/worker-manager/images/worker-base-codex/Dockerfile",
        "CODEX_CLI_VERSION",
        "codex-{version}-exec-help.txt",
    ),
    LLMChannel.CLAUDE: (
        REPO / "services/worker-manager/images/worker-base-claude/Dockerfile",
        "CLAUDE_CODE_VERSION",
        "claude-{version}-help.txt",
    ),
}


def _pin(channel: LLMChannel) -> str:
    dockerfile, arg, _ = PINS[channel]
    match = re.search(rf"^ARG {arg}=(\S+)$", dockerfile.read_text(), flags=re.MULTILINE)
    assert match is not None, f"{dockerfile} pins no {arg}"
    return match.group(1)


def _pinned_help(channel: LLMChannel) -> str:
    _, _, name = PINS[channel]
    path = HELP / name.format(version=_pin(channel))
    assert path.is_file(), f"no help output captured for the pinned version: {path.name}"
    return path.read_text()


@pytest.mark.parametrize("channel", list(PINS))
def test_the_pinned_cli_accepts_every_flag_its_adapter_passes(channel):
    assert missing_flags(channel, _pinned_help(channel)) == []


def test_the_codex_flags_the_card_names_are_the_ones_checked():
    flags = adapter_flags(LLMChannel.CODEX)

    for flag in (
        "--ephemeral",
        "--ignore-user-config",
        "--output-schema",
        "--output-last-message",
        "--sandbox",
        "-c",
    ):
        assert flag in flags
    command = adapter_command(LLMChannel.CODEX)
    assert command[command.index("--sandbox") + 1] == "read-only"
    assert command[-1] == "-"


@pytest.mark.parametrize(
    ("channel", "dropped", "named"),
    [
        (LLMChannel.CODEX, "--ephemeral", "--ephemeral"),
        (LLMChannel.CODEX, "`-` is used", "- (prompt on stdin)"),
        (LLMChannel.CLAUDE, "--no-session-persistence", "--no-session-persistence"),
    ],
)
def test_a_flag_the_cli_does_not_list_is_named(channel, dropped, named):
    help_text = _pinned_help(channel).replace(dropped, "")

    assert missing_flags(channel, help_text) == [named]


class TestImage:
    text = LANGGRAPH_DOCKERFILE.read_text()

    @pytest.mark.parametrize("channel", list(PINS))
    def test_the_version_is_read_from_the_worker_image_not_written_again(self, channel):
        dockerfile, arg, _ = PINS[channel]

        assert _pin(channel) not in self.text
        assert f"COPY {dockerfile.relative_to(REPO)} " in self.text
        assert f"s/^ARG {arg}=//p" in self.text

    def test_the_build_fails_when_an_installed_version_differs_from_the_pin(self):
        assert 'test "$(HOME="$home" codex --version)" = "codex-cli ${version}"' in self.text
        assert 'test "$(/opt/claude/claude --version)" = "$version (Claude Code)"' in self.text

    def test_the_build_checks_the_adapter_flags_against_the_installed_clis(self):
        copy_src = self.text.index("COPY services/langgraph/src ./src")

        assert self.text.index("RUN python -m src.llm.cli_contract") > copy_src
