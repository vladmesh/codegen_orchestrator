from dataclasses import dataclass

from .base import AgentRunner

# CLI overrides replace the mounted profile's policy without carrying values in
# argv. Only process settings and the developer's existing broker identity reach
# shell tools; model/App/GitHub credentials remain outside that environment.
DEVELOPER_SHELL_POLICY = (
    'shell_environment_policy={inherit="all",ignore_default_excludes=true,'
    "exclude=[],set={},experimental_use_profile=false,include_only=["
    '"HOME","LANG","LC_ALL","LC_CTYPE","PATH","PYTHONPATH",'
    '"PYTHONNOUSERSITE","TERM","TMPDIR","TZ",'
    '"WORKER_BROKER_URL","WORKER_BROKER_TOKEN","WORKER_ID"]}'
)


@dataclass
class CodexRunner(AgentRunner):
    """Runner for Codex CLI non-interactive developer work."""

    allow_non_git_workspace: bool = False

    def build_command(self, prompt: str) -> list[str]:
        # The container is the sandbox, so Codex must not try to build a second one
        # inside it. `workspace-write` runs every file read and every patch through
        # Codex's own bwrap helper, and bwrap needs a user namespace the worker
        # container does not have — it is denied there even without `cap_drop: ALL`
        # and `no-new-privileges`. Codex then fails on the first read of TASK.md,
        # answers that it is blocked by the sandbox and exits without writing a
        # result, which the wrapper can only report as `agent_exited_without_result`:
        # that is what killed both Codex worker cells and the Codex QA cell of the
        # 2026-08-13 production matrix.
        #
        # The isolation this drops is Codex's, not the worker's. The container keeps
        # every boundary the pre-alpha hardening gave it, and widening it instead —
        # SYS_ADMIN or an unconfined seccomp profile, to let bwrap nest — would trade
        # a real boundary for a redundant one.
        command = [
            "codex",
            "exec",
            "--sandbox",
            "danger-full-access",
        ]
        # A central QA worker intentionally receives an empty scratch directory,
        # not a checkout. This is Codex's native opt-in for that execution mode;
        # developer workers retain the usual repository check.
        if self.allow_non_git_workspace:
            command.append("--skip-git-repo-check")
        else:
            command.extend(["-c", DEVELOPER_SHELL_POLICY])
        return [*command, prompt]
