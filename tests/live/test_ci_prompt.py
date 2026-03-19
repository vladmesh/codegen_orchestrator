"""Step 6: CI fix prompt — Issue #6 (multiple pushes per fix session).

Unit test: verifies the CI fix prompt instructs the worker to run local
checks and push only once. Currently EXPECTED TO FAIL because the prompt
says "run local checks, commit and push" without specifying `make lint`
or requiring a single push.
"""

from pathlib import Path
import sys

# Add langgraph service to path so we can import _ci_gate
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "services" / "langgraph"))

from src.consumers._ci_gate import _build_ci_fix_prompt  # noqa: E402


class TestCiFixPromptContent:
    def test_prompt_requires_local_lint(self):
        """Prompt should tell worker to run `make lint` before pushing."""
        prompt = _build_ci_fix_prompt("lint failed", attempt=1)
        assert "make lint" in prompt.lower(), (
            "CI fix prompt does not mention `make lint`. "
            "Worker will push without local validation → multiple CI runs."
        )

    def test_prompt_requires_single_push(self):
        """Prompt should instruct worker to fix ALL issues before pushing."""
        prompt = _build_ci_fix_prompt("lint failed", attempt=1)
        lower = prompt.lower()
        has_all_issues = "all" in lower and (
            "issue" in lower or "failure" in lower or "error" in lower
        )
        has_single_push = "once" in lower or "single push" in lower or "one push" in lower
        assert has_all_issues or has_single_push, (
            "CI fix prompt does not instruct worker to fix ALL issues before pushing. "
            "Worker will push after each fix → wasted CI runs."
        )

    def test_prompt_contains_reject_instructions(self):
        """Prompt should contain REJECTED section instructions (already works)."""
        prompt = _build_ci_fix_prompt("build failed", attempt=1)
        assert "REJECTED" in prompt

    def test_prompt_includes_run_url_when_provided(self):
        """Prompt should include the run URL when given."""
        prompt = _build_ci_fix_prompt(
            "lint failed",
            attempt=1,
            run_url="https://github.com/org/repo/actions/runs/12345",
        )
        assert "12345" in prompt
