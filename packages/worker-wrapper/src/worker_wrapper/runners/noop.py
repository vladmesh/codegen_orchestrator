from dataclasses import dataclass

from .base import AgentRunner


@dataclass
class NoopRunner(AgentRunner):
    """Runner for E2E testing — empty commit + push, no LLM."""

    def build_command(self, prompt: str) -> list[str]:
        return [
            "bash",
            "-c",
            "git config core.hooksPath /dev/null && "
            'git commit --allow-empty -m "chore: noop marker for e2e test" && '
            "git push origin main && "
            "echo '<result>"
            '{"status": "success", "content": "noop commit pushed"}'
            "</result>'",
        ]
