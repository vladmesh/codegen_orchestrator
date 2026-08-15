"""Real-daemon regression for project-scoped deployment cleanup."""

from __future__ import annotations

import os
from pathlib import Path
import shlex
import subprocess
from uuid import uuid4

from shared.live_harness_cleanup import REMOTE_CLEANUP_SCRIPT, build_remote_cleanup_command


def _docker(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", *args],
        check=check,
        capture_output=True,
        text=True,
        timeout=120,
    )


def _exists(kind: str, identifier: str) -> bool:
    return _docker(kind, "inspect", identifier, check=False).returncode == 0


def _build_project_image(project: str, context: Path) -> str:
    tag = f"{project}-backend:cleanup"
    context.mkdir()
    (context / "Dockerfile").write_text(
        "\n".join(
            [
                "FROM alpine:3.20",
                f'LABEL com.docker.compose.project="{project}"',
                'CMD ["sleep", "300"]',
                "",
            ]
        ),
        encoding="utf-8",
    )
    _docker("build", "--pull", "-t", tag, str(context))
    return tag


def _run_cleanup(project: str, service_base: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        shlex.split(build_remote_cleanup_command(project, service_base=str(service_base))),
        input=REMOTE_CLEANUP_SCRIPT.read_text(),
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
        env=os.environ.copy(),
    )


def test_project_cleanup_reclaims_only_unreferenced_project_artifacts(tmp_path: Path):
    """A target cleanup cannot prune a live neighbour or reusable image tags."""
    suffix = uuid4().hex[:12]
    project = f"cleanup-{suffix}"
    neighbour = f"neighbour-{suffix}"
    retry_project = f"retry-{suffix}"
    service_base = tmp_path / "services"
    service_dir = service_base / project
    target_tag = _build_project_image(project, tmp_path / "target-image")
    neighbour_tag = _build_project_image(neighbour, tmp_path / "neighbour-image")
    retry_tag = _build_project_image(retry_project, tmp_path / "retry-image")
    target_container = f"{project}-backend-1"
    neighbour_container = f"{neighbour}-backend-1"
    retry_container = f"{retry_project}-backend-1"
    conflict_container = f"{neighbour}-uses-{retry_project}"
    project_volume = f"{project}-data"
    anonymous_volume = ""
    retry_anonymous_volume = ""
    reusable_tags = (f"postgres:cleanup-{suffix}", f"redis:cleanup-{suffix}")

    try:
        _docker("pull", "alpine:3.20")
        for reusable_tag in reusable_tags:
            _docker("tag", "alpine:3.20", reusable_tag)

        service_dir.mkdir(parents=True)
        _docker(
            "volume", "create", "--label", f"com.docker.compose.project={project}", project_volume
        )
        _docker(
            "run",
            "-d",
            "--name",
            target_container,
            "--label",
            f"com.docker.compose.project={project}",
            "--mount",
            "type=volume,destination=/owned",
            target_tag,
        )
        anonymous_volume = _docker(
            "inspect",
            "-f",
            '{{range .Mounts}}{{if eq .Type "volume"}}{{.Name}}{{end}}{{end}}',
            target_container,
        ).stdout.strip()
        assert anonymous_volume

        _docker(
            "run",
            "-d",
            "--name",
            neighbour_container,
            "--label",
            f"com.docker.compose.project={neighbour}",
            neighbour_tag,
        )

        result = _run_cleanup(project, service_base)

        assert result.returncode == 0, result.stderr
        assert not _exists("container", target_container)
        assert not _exists("image", target_tag)
        assert not _exists("volume", project_volume)
        assert not _exists("volume", anonymous_volume)
        assert not service_dir.exists()
        assert _exists("container", neighbour_container)
        assert _exists("image", neighbour_tag)
        for reusable_tag in reusable_tags:
            assert _exists("image", reusable_tag)

        retry = _run_cleanup(project, service_base)
        assert retry.returncode == 0, retry.stderr

        _docker(
            "run",
            "-d",
            "--name",
            retry_container,
            "--label",
            f"com.docker.compose.project={retry_project}",
            "--mount",
            "type=volume,destination=/retry-owned",
            retry_tag,
        )
        retry_anonymous_volume = _docker(
            "inspect",
            "-f",
            '{{range .Mounts}}{{if eq .Type "volume"}}{{.Name}}{{end}}{{end}}',
            retry_container,
        ).stdout.strip()
        assert retry_anonymous_volume
        _docker(
            "run",
            "-d",
            "--name",
            conflict_container,
            "--label",
            f"com.docker.compose.project={neighbour}",
            "--mount",
            f"type=volume,src={retry_anonymous_volume},destination=/shared",
            neighbour_tag,
        )

        conflict = _run_cleanup(retry_project, service_base)

        assert conflict.returncode != 0
        assert "remains referenced" in conflict.stderr
        assert (service_base / ".codegen-cleanup-candidates" / retry_project).exists()
        assert _exists("image", retry_tag)
        assert _exists("container", conflict_container)

        _docker("rm", "-f", conflict_container)
        recovered = _run_cleanup(retry_project, service_base)
        assert recovered.returncode == 0, recovered.stderr
        assert not _exists("image", retry_tag)
        assert not _exists("volume", retry_anonymous_volume)
        assert not (service_base / ".codegen-cleanup-candidates" / retry_project).exists()
    finally:
        for container in (
            target_container,
            neighbour_container,
            retry_container,
            conflict_container,
        ):
            _docker("rm", "-f", "-v", container, check=False)
        for volume in (project_volume, anonymous_volume, retry_anonymous_volume):
            if volume:
                _docker("volume", "rm", volume, check=False)
        for tag in (target_tag, neighbour_tag, retry_tag, *reusable_tags):
            _docker("image", "rm", "-f", tag, check=False)
