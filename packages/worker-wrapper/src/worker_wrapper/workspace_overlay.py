"""Turn-local files layered over a generated-product Git workspace.

The overlay is visible to the coding agent, but its state and ignore rules live
under ``.git``.  Completion removes the overlay from both the worktree and the
index before the wrapper publishes a commit.
"""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
from typing import Any

INSTRUCTION_START = "# --- codegen orchestrator workspace overlay: instructions ---"
INSTRUCTION_END = "# --- end codegen orchestrator workspace overlay: instructions ---"
MAKEFILE_START = "# --- orchestrator overrides ---"
MAKEFILE_END = "# --- end orchestrator overrides ---"
EXCLUDE_START = "# --- codegen orchestrator workspace overlay ---"
EXCLUDE_END = "# --- end codegen orchestrator workspace overlay ---"

CONTROL_PATHS = (
    "TASK.md",
    ".story",
    "PROGRESS.md",
    "REPORT.md",
    ".venv_paths_fixed",
    ".shebangs_fixed",
)

MAKEFILE_OVERRIDE = (
    f"\n{MAKEFILE_START}\n"
    "worker-start:\n"
    '\t@response="$$(curl -sS -f -X POST http://localhost:9090/infra/compose '
    "-H 'Content-Type: application/json' "
    '-d \'{"args": ["up", "-d", "--build", "--wait", "$(svc)"], '
    '"cwd": "."}\')"; status=$$?; [ $$status -eq 0 ] || exit $$status; '
    "printf '%s\\n' \"$$response\" | jq -er '.stderr // \"\"' >&2; "
    "status=$$?; [ $$status -eq 0 ] || exit $$status; "
    "printf '%s' \"$$response\" | jq -e '.exit_code == 0' >/dev/null\n"
    "\nworker-stop:\n"
    '\t@response="$$(curl -sS -f -X POST http://localhost:9090/infra/compose '
    "-H 'Content-Type: application/json' "
    '-d \'{"args": ["down", "--remove-orphans"], "cwd": "."}\')"; '
    "status=$$?; [ $$status -eq 0 ] || exit $$status; "
    "printf '%s\\n' \"$$response\" | jq -er '.stderr // \"\"' >&2; "
    "status=$$?; [ $$status -eq 0 ] || exit $$status; "
    "printf '%s' \"$$response\" | jq -e '.exit_code == 0' >/dev/null\n"
    f"{MAKEFILE_END}\n"
)


class WorkspaceOverlayError(RuntimeError):
    """The workspace could not be restored to a publishable product tree."""


class WorkspaceOverlay:
    """Own the control-plane overlay for one persistent worker checkout."""

    def __init__(self, workspace: str | Path):
        self.workspace = Path(workspace)
        self.git_dir = self.workspace / ".git"
        self.state_path = self.git_dir / "codegen-workspace-overlay.json"

    def configure_instruction(self, relative_path: str, content: str) -> None:
        """Record the agent-specific instruction overlay in Git-local state."""
        path = self.workspace / relative_path
        clean = self._strip_instruction(path.read_text() if path.is_file() else "")
        state = self._load_state()
        state.update(
            {
                "instruction_path": relative_path,
                "instruction_content": content,
                "instruction_existed": self._is_tracked(relative_path) or bool(clean),
            }
        )
        self._write_state(state)
        self._install_excludes(relative_path)

    def activate(self, *, task: str | None, story: str | None) -> None:
        """Expose instructions, task/story context and Compose proxy targets."""
        state = self._load_state()
        instruction_path = state.get("instruction_path")
        instruction_content = state.get("instruction_content")
        if isinstance(instruction_path, str) and isinstance(instruction_content, str):
            path = self.workspace / instruction_path
            product = self._strip_instruction(path.read_text() if path.is_file() else "")
            separator = "" if not product or product.endswith("\n") else "\n"
            self._write_text(
                path,
                f"{product}{separator}{INSTRUCTION_START}\n{instruction_content.rstrip()}\n"
                f"{INSTRUCTION_END}\n",
            )

        makefile = self.workspace / "Makefile"
        if not makefile.is_file():
            raise WorkspaceOverlayError(
                "Makefile is missing; cannot install worker compose proxy overrides"
            )
        product_makefile = self._strip_makefile(makefile.read_text())
        self._write_text(makefile, product_makefile.rstrip("\n") + "\n" + MAKEFILE_OVERRIDE)

        if task is not None:
            self._write_text(self.workspace / "TASK.md", task)
        if story is not None:
            self._write_text(self.workspace / ".story" / "STORY.md", story)
        self._install_excludes(instruction_path if isinstance(instruction_path, str) else None)

    def recover(self) -> str:
        """Remove a prior interrupted turn before another pull or activation."""
        return self.sanitize_commit()

    def venv_paths_fixed(self) -> bool:
        """Whether relocation already ran for this persistent checkout state."""
        return self._load_state().get("venv_paths_fixed") is True

    def mark_venv_paths_fixed(self) -> None:
        """Persist relocation state under ``.git``, never in the product tree."""
        state = self._load_state()
        state["venv_paths_fixed"] = True
        self._write_state(state)

    def sanitize_commit(self) -> str:
        """Remove control artifacts and amend their removal into local ``HEAD``."""
        try:
            state = self._load_state()
            instruction_path = state.get("instruction_path")
            if isinstance(instruction_path, str):
                path = self.workspace / instruction_path
                if path.is_file():
                    product = self._strip_instruction(path.read_text())
                    if product or state.get("instruction_existed"):
                        self._write_text(path, product)
                    else:
                        path.unlink()

            makefile = self.workspace / "Makefile"
            if makefile.is_file():
                self._write_text(makefile, self._strip_makefile(makefile.read_text()))

            for relative in CONTROL_PATHS:
                self._remove(self.workspace / relative)

            self._stage_overlay_paths(instruction_path)
            cached = self._git("diff", "--cached", "--quiet", "HEAD", check=False)
            if cached.returncode not in (0, 1):
                raise WorkspaceOverlayError("could not inspect the sanitized Git index")
            if cached.returncode == 1:
                self._git("commit", "--amend", "--no-edit", "--allow-empty")
            return self._git("rev-parse", "HEAD").stdout.strip()
        except WorkspaceOverlayError:
            raise
        except (OSError, subprocess.SubprocessError) as exc:
            raise WorkspaceOverlayError(f"workspace overlay cleanup failed: {exc}") from exc

    def _stage_overlay_paths(self, instruction_path: Any) -> None:
        candidates = ["Makefile", *CONTROL_PATHS]
        if isinstance(instruction_path, str):
            candidates.append(instruction_path)
        stage = []
        for relative in candidates:
            if (self.workspace / relative).exists() or self._git(
                "ls-files", "--", relative, check=False
            ).stdout.strip():
                stage.append(relative)
        if stage:
            self._git("add", "-A", "--", *stage)

    def _install_excludes(self, instruction_path: str | None) -> None:
        exclude = self.git_dir / "info" / "exclude"
        current = exclude.read_text() if exclude.is_file() else ""
        current = self._strip_section(current, EXCLUDE_START, EXCLUDE_END)
        patterns = [f"/{path}/" if path == ".story" else f"/{path}" for path in CONTROL_PATHS]
        if instruction_path:
            patterns.append(f"/{instruction_path}")
        block = "\n".join((EXCLUDE_START, *patterns, EXCLUDE_END, ""))
        separator = "" if not current or current.endswith("\n") else "\n"
        self._write_text(exclude, current + separator + block)

    def _load_state(self) -> dict[str, Any]:
        if not self.state_path.is_file():
            return {}
        try:
            value = json.loads(self.state_path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise WorkspaceOverlayError(f"workspace overlay state is unreadable: {exc}") from exc
        if not isinstance(value, dict):
            raise WorkspaceOverlayError("workspace overlay state is not an object")
        return value

    def _write_state(self, state: dict[str, Any]) -> None:
        self._write_text(self.state_path, json.dumps(state, sort_keys=True))

    def _is_tracked(self, relative_path: str) -> bool:
        return bool(self._git("ls-files", "--", relative_path, check=False).stdout.strip())

    def _git(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            ["/usr/bin/git", *args],
            cwd=self.workspace,
            capture_output=True,
            text=True,
            timeout=60,
        )
        if check and result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip() or "unknown Git error"
            raise WorkspaceOverlayError(f"git {' '.join(args)} failed: {detail}")
        return result

    @staticmethod
    def _write_text(path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)

    @classmethod
    def _strip_instruction(cls, content: str) -> str:
        return cls._strip_section(content, INSTRUCTION_START, INSTRUCTION_END)

    @classmethod
    def _strip_makefile(cls, content: str) -> str:
        if MAKEFILE_START not in content:
            return content
        if MAKEFILE_END not in content:
            return content.split(MAKEFILE_START, 1)[0].rstrip("\n") + "\n"
        return cls._strip_section(content, MAKEFILE_START, MAKEFILE_END)

    @staticmethod
    def _strip_section(content: str, start: str, end: str) -> str:
        while start in content:
            before, remainder = content.split(start, 1)
            if end not in remainder:
                return before.rstrip("\n") + ("\n" if before else "")
            _, after = remainder.split(end, 1)
            content = before.rstrip("\n") + after
        return content

    @staticmethod
    def _remove(path: Path) -> None:
        if path.is_dir() and not path.is_symlink():
            for child in sorted(path.iterdir(), reverse=True):
                WorkspaceOverlay._remove(child)
            path.rmdir()
        elif path.exists() or path.is_symlink():
            path.unlink()
