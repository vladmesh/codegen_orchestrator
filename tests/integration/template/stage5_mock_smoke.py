"""Canonical service-template compatibility smoke harness."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tempfile
from uuid import uuid4

import yaml

ROOT = Path(__file__).resolve().parents[3]
SYSTEM_CONFIG = ROOT / "scripts" / "system_configs.yaml"
COMPOSE_LABEL = "com.docker.compose.project"
COMMAND_TIMEOUT_SECONDS = 20 * 60
SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$", re.IGNORECASE)


def _set_standard_umask() -> None:
    os.umask(0o022)


@dataclass(frozen=True)
class TemplateRevision:
    source: str
    ref: str


class CommandTimeout(RuntimeError):
    """A smoke phase exceeded its explicit command timeout."""


def load_production_template(path: Path = SYSTEM_CONFIG) -> TemplateRevision:
    """Read the production source and pin from the scheduler seed config."""
    values = {item["key"]: item["value"] for item in yaml.safe_load(path.read_text())}
    return TemplateRevision(
        source=str(values["scheduler.service_template_source"]),
        ref=str(values["scheduler.service_template_ref"]),
    )


@dataclass(frozen=True)
class Stage5Smoke:
    """Run one requested template revision's full worker-mode contract."""

    workspace: Path
    compose_project_name: str
    template: TemplateRevision
    artifact: Path
    command_timeout: int = COMMAND_TIMEOUT_SECONDS

    @classmethod
    def create(
        cls,
        temporary_root: Path,
        *,
        source: str,
        ref: str,
        artifact: Path | None = None,
    ) -> Stage5Smoke:
        run_id = uuid4().hex
        return cls(
            workspace=temporary_root / f"stage5-template-{run_id}",
            compose_project_name=f"codegen_stage5_{run_id}",
            template=TemplateRevision(source=source, ref=ref),
            artifact=artifact or temporary_root / "template-compat-result.json",
        )

    def run(self) -> str:
        resolved_commit: str | None = None
        error: str | None = None
        self.workspace.mkdir(parents=True)
        try:
            resolved_commit = self._resolve_remote_ref(self.template.ref)
            self._run_copier(resolved_commit)
            self._read_resolved_commit(resolved_commit)
            self._run_make("setup")
            self._run_make("lint")
            self._run_make("tests")
            self._make_workspace_readable()
            self._run_worker_start()
            self._run_make(
                "smoke-probe",
                "SMOKE_RUNNER=backend",
                "SMOKE_URL=http://backend:8000/health",
            )
            self._run_make(
                "worker-call",
                "SMOKE_RUNNER=backend",
                "url=http://backend:8000/users",
                "method=POST",
                'body={"telegram_id":5001}',
            )
            return resolved_commit
        except Exception as caught:
            error = str(caught)
            raise
        finally:
            try:
                self.cleanup()
            except Exception as cleanup_error:
                error = f"{error}\ncleanup: {cleanup_error}" if error else str(cleanup_error)
                raise
            finally:
                self._write_artifact(resolved_commit, error)

    def cleanup(self) -> None:
        compose_file = self.workspace / "infra" / "compose.base.yml"
        if compose_file.exists():
            self._run(
                [
                    "docker",
                    "compose",
                    "-p",
                    self.compose_project_name,
                    "-f",
                    "infra/compose.base.yml",
                    "-f",
                    "infra/compose.dev.yml",
                    "down",
                    "--volumes",
                    "--remove-orphans",
                ],
                cwd=self.workspace,
                phase="cleanup",
            )
        self._assert_no_compose_resources()
        shutil.rmtree(self.workspace, ignore_errors=True)

    def _run_copier(self, resolved_commit: str) -> None:
        self._run(
            [
                "copier",
                "copy",
                "--defaults",
                f"--vcs-ref={resolved_commit}",
                "--data",
                "project_name=stage5-smoke",
                "--data",
                "modules=backend",
                "--data",
                "task_description=deterministic local contract smoke",
                self.template.source,
                str(self.workspace),
            ],
            phase="scaffold",
        )

    def _read_resolved_commit(self, expected_commit: str) -> str:
        answers = yaml.safe_load((self.workspace / ".copier-answers.yml").read_text())
        commit = answers.get("_commit") if isinstance(answers, dict) else None
        if not isinstance(commit, str) or not self._recorded_ref_matches(commit, expected_commit):
            raise RuntimeError(
                f"Copier resolved unexpected commit: expected={expected_commit!r}, "
                f"recorded={commit!r}"
            )
        return expected_commit

    def _recorded_ref_matches(self, recorded: str, resolved: str) -> bool:
        if SHA_PATTERN.fullmatch(recorded):
            return recorded.lower() == resolved
        if recorded == self.template.ref:
            return True
        describe_match = re.search(r"-g([0-9a-f]{7,40})$", recorded, re.IGNORECASE)
        return bool(
            SHA_PATTERN.fullmatch(self.template.ref)
            and describe_match
            and resolved.startswith(describe_match.group(1).lower())
        )

    def _resolve_remote_ref(self, ref: str) -> str:
        with tempfile.TemporaryDirectory(
            prefix="template-ref-", dir=self.workspace.parent
        ) as repository:
            self._run(["git", "init", "--bare", repository], phase="initialize ref resolver")
            self._run(
                [
                    "git",
                    "-C",
                    repository,
                    "fetch",
                    "--depth=1",
                    self._git_source(),
                    ref,
                ],
                phase="fetch template ref",
            )
            result = self._run(
                ["git", "-C", repository, "rev-parse", "FETCH_HEAD^{commit}"],
                phase="resolve template ref",
            )
        resolved = result.stdout.strip().lower()
        if not SHA_PATTERN.fullmatch(resolved):
            raise RuntimeError(
                f"template ref cannot be resolved to a commit SHA: {self.template.source}@{ref}"
            )
        if SHA_PATTERN.fullmatch(ref) and resolved != ref.lower():
            raise RuntimeError(
                f"template commit mismatch: requested={ref.lower()}, resolved={resolved}"
            )
        return resolved

    def _git_source(self) -> str:
        prefix = "gh:"
        if self.template.source.startswith(prefix):
            return f"https://github.com/{self.template.source.removeprefix(prefix)}.git"
        return self.template.source

    def _write_artifact(self, resolved_commit: str | None, error: str | None) -> None:
        self.artifact.parent.mkdir(parents=True, exist_ok=True)
        self.artifact.write_text(
            json.dumps(
                {
                    "requested_source": self.template.source,
                    "requested_ref": self.template.ref,
                    "resolved_commit": resolved_commit,
                    "outcome": "failed" if error else "passed",
                    "error": error,
                    "compose_project_name": self.compose_project_name,
                },
                indent=2,
            )
            + "\n"
        )

    def _run_make(self, target: str, *variables: str) -> None:
        self._run(["make", target, *variables], cwd=self.workspace, phase=target)

    def _run_worker_start(self) -> None:
        try:
            self._run_make("worker-start")
        except RuntimeError as error:
            logs = self._run(
                [
                    "docker",
                    "compose",
                    "-p",
                    self.compose_project_name,
                    "-f",
                    "infra/compose.base.yml",
                    "-f",
                    "infra/compose.dev.yml",
                    "logs",
                    "--no-color",
                ],
                cwd=self.workspace,
                check=False,
                phase="worker-start logs",
            )
            raise RuntimeError(f"{error}\ncompose logs:\n{logs.stdout}\n{logs.stderr}") from error

    def _make_workspace_readable(self) -> None:
        for path in (self.workspace, *self.workspace.rglob("*")):
            if path.is_symlink():
                continue
            mode = path.stat().st_mode
            readable_mode = mode | stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH
            if path.is_dir():
                readable_mode |= stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
            path.chmod(readable_mode)

    def _assert_no_compose_resources(self) -> None:
        for resource, args in (
            ("containers", ["docker", "ps", "-aq"]),
            ("networks", ["docker", "network", "ls", "-q"]),
            ("volumes", ["docker", "volume", "ls", "-q"]),
        ):
            result = self._run(
                [*args, "--filter", f"label={COMPOSE_LABEL}={self.compose_project_name}"],
                phase=f"verify cleanup {resource}",
            )
            if result.stdout.strip():
                raise AssertionError(f"Stage 5 smoke left {resource}: {result.stdout.strip()}")

    def _run(
        self,
        command: list[str],
        *,
        cwd: Path | None = None,
        check: bool = True,
        phase: str = "command",
    ) -> subprocess.CompletedProcess[str]:
        workspace_owner = self.workspace.parent.stat()
        environment = os.environ | {
            "COMPOSE_PROJECT_NAME": self.compose_project_name,
            "HOST_UID": str(workspace_owner.st_uid),
            "HOST_GID": str(workspace_owner.st_gid),
        }
        environment.pop("VIRTUAL_ENV", None)
        try:
            result = subprocess.run(
                command,
                cwd=cwd,
                env=environment,
                check=False,
                text=True,
                capture_output=True,
                preexec_fn=_set_standard_umask,
                timeout=self.command_timeout,
            )
        except subprocess.TimeoutExpired as error:
            raise CommandTimeout(
                f"Phase {phase} timed out after {self.command_timeout}s: {' '.join(command)}"
            ) from error
        if check and result.returncode:
            raise RuntimeError(
                f"Phase {phase} failed ({result.returncode}): {' '.join(command)}\n"
                f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
            )
        return result


def main() -> None:
    production = load_production_template()
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", default=production.source)
    parser.add_argument("--ref", default=production.ref)
    parser.add_argument("--artifact", type=Path, required=True)
    parser.add_argument("--workspace-root", type=Path, required=True)
    args = parser.parse_args()
    smoke = Stage5Smoke.create(
        args.workspace_root, source=args.source, ref=args.ref, artifact=args.artifact
    )
    resolved = smoke.run()
    print(
        f"template compatibility passed: requested={args.source}@{args.ref} "
        f"resolved={resolved} artifact={args.artifact}"
    )


if __name__ == "__main__":
    main()
