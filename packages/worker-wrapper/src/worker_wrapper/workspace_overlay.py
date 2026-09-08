"""Turn-local files layered over a generated-product Git workspace.

The overlay is visible to the coding agent, but its state and containment rules
live under ``.git``. Completion rewrites only unpublished commits through an
isolated index before the wrapper publishes them.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile
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
        self._set_overlay_index_flags(instruction_path, enabled=True)
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
        """Remove tracked overlays without changing HEAD, index, or retained context."""
        self.deactivate()
        return self._git("rev-parse", "HEAD").stdout.strip()

    def deactivate(self) -> None:
        """Restore product-owned tracked files while retaining turn context for retry."""
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
            self._set_overlay_index_flags(instruction_path, enabled=False)
        except WorkspaceOverlayError:
            raise
        except OSError as exc:
            raise WorkspaceOverlayError(f"workspace overlay deactivation failed: {exc}") from exc

    def venv_paths_fixed(self) -> bool:
        """Whether relocation already ran for this persistent checkout state."""
        return self._load_state().get("venv_paths_fixed") is True

    def mark_venv_paths_fixed(self) -> None:
        """Persist relocation state under ``.git``, never in the product tree."""
        state = self._load_state()
        state["venv_paths_fixed"] = True
        self._write_state(state)

    def sanitize_unpublished_commits(self, branch: str, expected_head: str) -> str:
        """Rewrite only the unpublished linear range through an isolated index.

        The caller has already established that ``expected_head`` is the agent's
        reported HEAD. The real index and worktree are not inputs to the new
        commits, so staged product WIP and persistent context cannot be absorbed.
        """
        state = self._load_state()
        instruction_path = state.get("instruction_path")
        overlay_paths = self._overlay_paths(instruction_path)
        actual_head = self._git("rev-parse", "--verify", "HEAD^{commit}").stdout.strip()
        if actual_head != expected_head:
            raise WorkspaceOverlayError("local HEAD changed before workspace sanitation")

        remote = self._git(
            "ls-remote", "--exit-code", "--heads", "origin", f"refs/heads/{branch}", check=False
        )
        remote_head = remote.stdout.split(maxsplit=1)[0] if remote.stdout.strip() else ""
        if remote.returncode != 0 or not remote_head:
            raise WorkspaceOverlayError(f"origin/{branch} could not be resolved before sanitation")
        if self._git(
            "merge-base", "--is-ancestor", remote_head, expected_head, check=False
        ).returncode:
            raise WorkspaceOverlayError(
                f"local HEAD does not descend from the published origin/{branch}"
            )

        staged_overlay = self._git(
            "diff", "--cached", "--quiet", expected_head, "--", *overlay_paths, check=False
        )
        if staged_overlay.returncode not in (0, 1):
            raise WorkspaceOverlayError("could not inspect overlay paths in the ambient index")
        if staged_overlay.returncode == 1:
            raise WorkspaceOverlayError(
                "overlay paths contain staged changes outside the reported commit"
            )

        rows = self._git(
            "rev-list", "--reverse", "--topo-order", "--parents", f"{remote_head}..{expected_head}"
        ).stdout.splitlines()
        if not rows:
            self._verify_clean_commit(expected_head, instruction_path)
            return expected_head

        rewritten_parent = remote_head
        old_parent = remote_head
        with tempfile.TemporaryDirectory(prefix="codegen-overlay-") as temp_dir:
            index_path = Path(temp_dir) / "index"
            for row in rows:
                fields = row.split()
                old_commit, parents = fields[0], fields[1:]
                if parents != [old_parent]:
                    raise WorkspaceOverlayError(
                        "workspace sanitation supports only an unpublished linear commit range"
                    )
                tree = self._sanitized_tree(old_commit, instruction_path, state, index_path)
                rewritten_parent = self._copy_commit(old_commit, tree, rewritten_parent)
                old_parent = old_commit

        for commit in self._git(
            "rev-list", "--reverse", f"{remote_head}..{rewritten_parent}"
        ).stdout.splitlines():
            self._verify_clean_commit(commit, instruction_path)

        branch_ref = f"refs/heads/{branch}"
        self._git("update-ref", branch_ref, rewritten_parent, expected_head)
        self._reset_overlay_index_paths(rewritten_parent, overlay_paths)
        self._set_overlay_index_flags(instruction_path, enabled=True)
        return rewritten_parent

    def _sanitized_tree(
        self, commit: str, instruction_path: Any, state: dict[str, Any], index_path: Path
    ) -> str:
        env = {**os.environ, "GIT_INDEX_FILE": str(index_path)}
        self._run_git(["read-tree", f"{commit}^{{tree}}"], env=env)
        self._run_git(
            ["rm", "-r", "-f", "--cached", "--ignore-unmatch", "--", *CONTROL_PATHS],
            env=env,
        )
        if isinstance(instruction_path, str):
            self._sanitize_blob_in_index(
                commit,
                instruction_path,
                self._strip_instruction,
                env,
                remove_empty=not state.get("instruction_existed", False),
            )
        self._sanitize_blob_in_index(commit, "Makefile", self._strip_makefile, env)
        return self._run_git(["write-tree"], env=env).stdout.strip()

    def _sanitize_blob_in_index(
        self,
        commit: str,
        relative_path: str,
        sanitizer,
        env: dict[str, str],
        *,
        remove_empty: bool = False,
    ) -> None:
        blob = self._git("show", f"{commit}:{relative_path}", check=False)
        if blob.returncode != 0:
            return
        clean = sanitizer(blob.stdout)
        if remove_empty and not clean:
            self._run_git(["rm", "--cached", "--ignore-unmatch", "--", relative_path], env=env)
            return
        try:
            mode, _kind, _object, _path = self._git(
                "ls-tree", commit, "--", relative_path
            ).stdout.split(maxsplit=3)
        except ValueError as exc:
            raise WorkspaceOverlayError(
                f"could not resolve {relative_path} in commit {commit}"
            ) from exc

        clean_blob = self._run_git(
            ["hash-object", "-w", "--stdin"], input_text=clean
        ).stdout.strip()
        self._run_git(
            ["update-index", "--add", "--cacheinfo", mode, clean_blob, relative_path], env=env
        )

    def _copy_commit(self, commit: str, tree: str, parent: str) -> str:
        raw = self._git(
            "show", "-s", "--format=%an%x00%ae%x00%aI%x00%cn%x00%ce%x00%cI%x00%B", commit
        ).stdout
        (
            author_name,
            author_email,
            author_date,
            committer_name,
            committer_email,
            committer_date,
            message,
        ) = raw.split("\0", 6)
        env = {
            **os.environ,
            "GIT_AUTHOR_NAME": author_name,
            "GIT_AUTHOR_EMAIL": author_email,
            "GIT_AUTHOR_DATE": author_date,
            "GIT_COMMITTER_NAME": committer_name,
            "GIT_COMMITTER_EMAIL": committer_email,
            "GIT_COMMITTER_DATE": committer_date,
        }
        return self._run_git(
            ["commit-tree", tree, "-p", parent], env=env, input_text=message
        ).stdout.strip()

    def _verify_clean_commit(self, commit: str, instruction_path: Any) -> None:
        names = set(self._git("ls-tree", "-r", "--name-only", commit).stdout.splitlines())
        forbidden = {
            name
            for name in names
            if name in CONTROL_PATHS or any(name.startswith(f"{root}/") for root in CONTROL_PATHS)
        }
        if forbidden:
            raise WorkspaceOverlayError(
                f"commit {commit} retains workspace control paths: {sorted(forbidden)}"
            )
        for relative_path, marker in (
            (instruction_path, INSTRUCTION_START),
            ("Makefile", MAKEFILE_START),
        ):
            if not isinstance(relative_path, str):
                continue
            content = self._git("show", f"{commit}:{relative_path}", check=False)
            if content.returncode == 0 and marker in content.stdout:
                raise WorkspaceOverlayError(
                    f"commit {commit} retains workspace overlay text in {relative_path}"
                )

    def _reset_overlay_index_paths(self, head: str, overlay_paths: list[str]) -> None:
        self._git("reset", "-q", head, "--", *overlay_paths)

    def _set_overlay_index_flags(self, instruction_path: Any, *, enabled: bool) -> None:
        flag = "--skip-worktree" if enabled else "--no-skip-worktree"
        for relative_path in self._overlay_paths(instruction_path)[:2]:
            if self._is_tracked(relative_path):
                self._git("update-index", flag, "--", relative_path)

    @staticmethod
    def _overlay_paths(instruction_path: Any) -> list[str]:
        paths = ["Makefile"]
        if isinstance(instruction_path, str):
            paths.insert(0, instruction_path)
        return [*paths, *CONTROL_PATHS]

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
        result = self._run_git(list(args), check=False)
        if check and result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip() or "unknown Git error"
            raise WorkspaceOverlayError(f"git {' '.join(args)} failed: {detail}")
        return result

    def _run_git(
        self,
        args: list[str],
        *,
        env: dict[str, str] | None = None,
        input_text: str | None = None,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        try:
            result = subprocess.run(
                ["/usr/bin/git", *args],
                cwd=self.workspace,
                env=env,
                input=input_text,
                capture_output=True,
                text=True,
                timeout=60,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise WorkspaceOverlayError(f"git {' '.join(args)} could not run: {exc}") from exc
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
