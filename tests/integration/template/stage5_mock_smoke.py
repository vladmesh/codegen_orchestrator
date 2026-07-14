"""Harness for the Stage 5 service-template smoke."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import shutil
import subprocess
from uuid import uuid4

TEMPLATE = "gh:vladmesh/service-template"
TEMPLATE_REF = "0.3.0"
COMPOSE_LABEL = "com.docker.compose.project"


@dataclass(frozen=True)
class Stage5Smoke:
    """Run the generated project's worker-mode contract in isolation."""

    workspace: Path
    compose_project_name: str
    template: str = TEMPLATE
    template_ref: str = TEMPLATE_REF

    @classmethod
    def create(cls, temporary_root: Path) -> Stage5Smoke:
        run_id = uuid4().hex
        return cls(
            workspace=temporary_root / f"stage5-template-{run_id}",
            compose_project_name=f"codegen_stage5_{run_id}",
        )

    def run(self) -> None:
        self.workspace.mkdir(parents=True)
        try:
            self._run_copier()
            self._run_make("setup")
            self._run_make("lint")
            self._run_make("tests")
            self._run_make("worker-start")
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
        finally:
            self.cleanup()

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
                check=False,
            )
        self._assert_no_compose_resources()
        shutil.rmtree(self.workspace, ignore_errors=True)

    def _run_copier(self) -> None:
        self._run(
            [
                "copier",
                "copy",
                "--defaults",
                f"--vcs-ref={self.template_ref}",
                "--data",
                "project_name=stage5-smoke",
                "--data",
                "modules=backend",
                "--data",
                "task_description=deterministic local contract smoke",
                self.template,
                str(self.workspace),
            ]
        )

    def _run_make(self, target: str, *variables: str) -> None:
        self._run(["make", target, *variables], cwd=self.workspace)

    def _assert_no_compose_resources(self) -> None:
        for resource, args in (
            ("containers", ["docker", "ps", "-aq"]),
            ("networks", ["docker", "network", "ls", "-q"]),
            ("volumes", ["docker", "volume", "ls", "-q"]),
        ):
            result = self._run(
                [*args, "--filter", f"label={COMPOSE_LABEL}={self.compose_project_name}"],
                check=False,
            )
            if result.stdout.strip():
                raise AssertionError(f"Stage 5 smoke left {resource}: {result.stdout.strip()}")

    def _run(
        self,
        command: list[str],
        *,
        cwd: Path | None = None,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        environment = os.environ | {"COMPOSE_PROJECT_NAME": self.compose_project_name}
        environment.pop("VIRTUAL_ENV", None)
        result = subprocess.run(
            command,
            cwd=cwd,
            env=environment,
            check=False,
            text=True,
            capture_output=True,
        )
        if check and result.returncode:
            raise RuntimeError(
                f"Command failed ({result.returncode}): {' '.join(command)}\n"
                f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
            )
        return result
