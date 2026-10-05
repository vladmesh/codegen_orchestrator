"""Mechanical catalog executor on an exclusively owned, clean story branch."""

import asyncio
from dataclasses import dataclass, field
import fcntl
import hashlib
import json
from pathlib import Path
import re

from shared.contracts.dto.catalog_install import InstallCommand, InstallVerification
from shared.diagnostics import redact_diagnostic
from src.scaffold import _git_auth_env, _run_cmd, _workspace_path


class InstallExecutionError(RuntimeError):
    def __init__(self, stage, detail, head_sha=None, base_sha=None):
        self.stage, self.head_sha, self.base_sha = stage, head_sha, base_sha
        super().__init__(detail)


@dataclass
class InstallResult:
    head_sha: str
    base_sha: str
    evidence: dict
    protected_sha256: dict[str, str]
    stages: list[dict] = field(default_factory=list)


def protected_files(root, tracked):
    selected = set()
    for name in tracked:
        parts = Path(name).parts
        if (
            name in {".copier-answers.yml", "pyproject.toml", "uv.lock"}
            or Path(name).name in {".env", ".env.example"}
            or "specs" in parts
            or "spec" in parts
            or any(part in parts for part in ("app", "controllers", "handlers"))
            or "bindings" in parts
            and "generated" not in parts
        ):
            selected.add(name)
    for prefix in ("", "services/backend/", "services/tg_bot/"):
        for filename in (".env", ".env.example"):
            if (root / f"{prefix}{filename}").is_file():
                selected.add(f"{prefix}{filename}")
    return {
        name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in sorted(selected)
    }


def install_environment(token, root):
    auth = _git_auth_env(token)
    permitted = {
        "HOME",
        "PATH",
        "LANG",
        "LC_ALL",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "GIT_CONFIG_COUNT",
        "GIT_CONFIG_KEY_0",
        "GIT_CONFIG_VALUE_0",
    }
    env = {key: value for key, value in auth.items() if key in permitted}
    env.update(
        {"GIT_TERMINAL_PROMPT": "0", "UV_NO_PROGRESS": "1", "VIRTUAL_ENV": str(root / ".venv")}
    )
    env["PATH"] = f"{root / '.venv/bin'}:{env['PATH']}"
    return env


async def run_install(msg, settings, git_url, token, fence) -> InstallResult:  # noqa: C901, PLR0915  # fixed stages share a workspace lease and retained head
    root = _workspace_path(settings.workspace_base_path, msg.repository_id)
    if not (root / ".git").is_dir():
        raise InstallExecutionError(
            "preflight", "workspace_unowned: ensure the owned repository first"
        )
    if not re.fullmatch(r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?:\.git)?", git_url):
        raise InstallExecutionError(
            "preflight", "repository_unowned: expected an owned GitHub HTTPS URL"
        )
    lock_dir = root.parent / ".catalog-install-locks"
    lock_dir.mkdir(exist_ok=True)
    lock = (lock_dir / msg.repository_id).open("a")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as error:
        lock.close()
        raise InstallExecutionError(
            "preflight", "branch_writer_live: workspace lock is held"
        ) from error
    env = install_environment(token, root)
    stage, base, head = "preflight", None, None
    stages = []
    verification = None

    async def checkpoint(action="checkpoint"):
        await fence(
            InstallCommand(
                action=action, stage=stage, base_sha=base, head_sha=head, verification=verification
            )
        )

    async def command(args, *, allow_absent=False, command_env=None):
        await checkpoint()
        rc, out, err = await _run_cmd(
            args, cwd=root, env=command_env or env, timeout=600, kill_process_group=True
        )
        stages.append({"stage": stage, "argv": args, "returncode": rc})
        if rc != 0 and not (allow_absent and rc == 1):
            raise InstallExecutionError(
                stage, redact_diagnostic(err or out, secrets=(token,))[:2000], head, base
            )
        return rc, out.strip()

    async def remote_head():
        _, output = await command(["git", "ls-remote", "--heads", "origin", f"refs/heads/{branch}"])
        return output.split()[0] if output else None

    try:
        _, dirty = await command(["git", "status", "--porcelain", "--untracked-files=all"])
        if dirty:
            raise InstallExecutionError(stage, "workspace_dirty: retained checkout needs review")
        _, origin = await command(["git", "remote", "get-url", "origin"])
        if origin.removesuffix(".git").rstrip("/") != git_url.removesuffix(".git").rstrip("/"):
            raise InstallExecutionError(
                stage, "repository_unowned: origin differs from the durable repository"
            )
        _, tracked_text = await command(["git", "ls-files"])
        tracked = tracked_text.splitlines()
        _, ignored_rejections = await command(
            [
                "git",
                "ls-files",
                "--others",
                "--ignored",
                "--exclude-standard",
                "--",
                "*.rej",
                "*.orig",
            ]
        )
        if ignored_rejections or any(name.endswith((".rej", ".orig")) for name in tracked):
            raise InstallExecutionError(
                stage, "update_unresolved: review Copier rejection artifacts"
            )
        branch = f"story/{msg.story_id}"
        await command(["git", "check-ref-format", "--branch", branch])
        await command(["git", "fetch", "--no-tags", "origin"])
        remote = await remote_head()
        rc, _ = await command(
            ["git", "show-ref", "--quiet", "--verify", f"refs/heads/{branch}"], allow_absent=True
        )
        if rc == 0:
            _, local = await command(["git", "rev-parse", f"refs/heads/{branch}"])
            if local != remote:
                raise InstallExecutionError(
                    stage, "branch_unpublished: retain the existing local story head"
                )
            await command(["git", "switch", branch])
        elif remote:
            await command(["git", "switch", "--create", branch, "--track", f"origin/{branch}"])
        else:
            await command(["git", "switch", "--create", branch, "origin/main"])
        _, base = await command(["git", "rev-parse", "HEAD"])
        await checkpoint()
        _, tracked_text = await command(["git", "ls-files"])
        protected = protected_files(root, tracked_text.splitlines())
        payload = msg.install.model_dump_json()
        probe = str(Path(__file__).with_name("install_probe.py"))
        python = str(root / ".venv/bin/python")
        _, preflight = await command([python, "-I", probe, "preflight", payload, msg.template_ref])
        json.loads(preflight)
        # Each argv is platform-owned. No command, artifact, source override or
        # product path comes from task prose. The product CLI owns all mutation.
        kit = str(root / ".venv/bin/kit")
        stage = "package"
        await command([kit, "add", msg.install.package.name])
        stage = "library"
        for library in msg.install.libraries:
            await command([kit, "add", library.name])
        stage = "bind"
        await command([kit, "bind", msg.install.package.name, "--default"])
        stage = "generate"
        await command(["make", "generate-from-spec"])
        stage = "validate"
        await command(["make", "validate-specs"])
        # The released make typecheck loop returns its last service's status.
        # Check each product interpreter directly so an earlier failure refuses.
        for service in ("backend", "tg_bot"):
            await command(
                [str(root / f"services/{service}/.venv/bin/mypy"), f"services/{service}"],
                command_env=env | {"PYTHONPATH": ".", "MYPYPATH": "."},
            )
        await command(["make", "tests"])
        stage = "readback"
        _, readback = await command([python, "-I", probe, "readback", payload, msg.template_ref])
        evidence = json.loads(readback)
        changed = [
            name
            for name, digest in protected.items()
            if not (root / name).is_file()
            or hashlib.sha256((root / name).read_bytes()).hexdigest() != digest
        ]
        if changed:
            raise InstallExecutionError(
                stage, "protected_files_changed: " + ", ".join(changed), head, base
            )
        verification = InstallVerification(
            core_version=evidence["core"],
            tooling_commit=evidence["tooling"]["vcs_info"]["commit_id"],
            binding_sha256=evidence["binding_sha256"],
            distributions={
                name: item["version"] for name, item in evidence["distributions"].items()
            },
            component_targets={
                item["name"]: item["target"] for item in evidence["component_sources"]
            },
            protected_sha256=protected,
        )
        stage = "commit"
        await command(["git", "add", "-A"])
        _, dirty = await command(["git", "status", "--porcelain"])
        if not dirty:
            raise InstallExecutionError(
                stage, "no_install_change: selected closure already exists", head, base
            )
        await command(
            [
                "git",
                "-c",
                "user.name=Codegen Bot",
                "-c",
                "user.email=codegen@localhost",
                "-c",
                "core.hooksPath=/dev/null",
                "commit",
                "-m",
                f"Install catalog package {msg.install.package.name} ({msg.operation_id})",
            ]
        )
        _, head = await command(["git", "rev-parse", "HEAD"])
        await checkpoint()
        stage = "push"
        if await remote_head() != remote:
            raise InstallExecutionError(
                stage, "branch_changed: remote head differs from the install base", head, base
            )
        # A lost push response is observed, never inferred. One immediate remote
        # read can prove it landed; otherwise the retained exact head is parked.
        await checkpoint()
        rc, out, err = await _run_cmd(
            ["git", "push", "origin", f"HEAD:refs/heads/{branch}"],
            cwd=root,
            env=env,
            timeout=600,
            kill_process_group=True,
        )
        stages.append(
            {
                "stage": stage,
                "argv": ["git", "push", "origin", f"HEAD:refs/heads/{branch}"],
                "returncode": rc,
            }
        )
        if await remote_head() != head:
            raise InstallExecutionError(
                stage,
                "push_outcome_unknown: " + redact_diagnostic(err or out, secrets=(token,))[:1500],
                head,
                base,
            )
        await checkpoint("publish")
        return InstallResult(
            head_sha=head,
            base_sha=base,
            evidence=evidence,
            protected_sha256=protected,
            stages=stages,
        )
    except asyncio.CancelledError:
        raise
    except InstallExecutionError:
        raise
    except Exception as error:
        raise InstallExecutionError(
            stage, redact_diagnostic(error, secrets=(token,))[:2000], head, base
        ) from error
    finally:
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()
