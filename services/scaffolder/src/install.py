"""Mechanical catalog executor on an exclusively owned, clean story branch."""

import asyncio
from dataclasses import dataclass, field
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shlex

from shared.constants import WorkerWorkspace
from shared.contracts.dto.catalog_install import InstallCommand, InstallVerification
from shared.diagnostics import redact_diagnostic
from shared.workspace_preservation import acquire_install_workspace_lock
from src.scaffold import _git_auth_env, _run_cmd, _workspace_path

#: Where a developer worker mounts this same checkout.
WORKER_WORKSPACE = "/workspace"
#: The product Makefile exports its .env, whose ``redis://redis:6379`` is the
#: orchestrator Redis on this network; the kit's unit leg runs against a reserved
#: TLD that never resolves, so a runtime reaching for Redis fails fast.
UNIT_LEG_REDIS_URL = "redis://redis.invalid:6379"
COMMAND_TIMEOUT = 600


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
            or name.startswith("services/tg_bot/src/")
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


def product_environment(root):
    permitted = {
        "HOME",
        "PATH",
        "LANG",
        "LC_ALL",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
    }
    env = {key: value for key, value in os.environ.items() if key in permitted}
    env.update(
        {"GIT_TERMINAL_PROMPT": "0", "UV_NO_PROGRESS": "1", "VIRTUAL_ENV": str(root / ".venv")}
    )
    env["PATH"] = f"{root / '.venv/bin'}:{env['PATH']}"
    # The scaffolder runs as root on a checkout worker-manager chowned to the
    # worker user; git and the product's own git calls refuse it as dubious.
    env.update(
        {
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "safe.directory",
            "GIT_CONFIG_VALUE_0": str(root),
        }
    )
    return env


def reclaim_worker_venvs(root):
    """Point venvs a worker repointed at its /workspace mount back at this checkout.

    The worker wrapper rewrites shebangs, ``.pth`` and ``direct_url.json`` to
    ``/workspace/``; executed here, ``.venv/bin/kit`` fails with ENOENT on its
    interpreter. Dropping the sentinel makes the next worker repoint them again.
    """
    mounted, own = f"{WORKER_WORKSPACE}/".encode(), f"{root}/".encode()
    for path in root.glob("**/.venv/bin/*"):
        if path.is_symlink() or not path.is_file():
            continue
        content = path.read_bytes()
        if content.startswith(b"#!" + mounted):
            path.write_bytes(b"#!" + own + content[2 + len(mounted) :])
    site = "**/.venv/lib/*/site-packages/"
    for path in [*root.glob(f"{site}_*.pth"), *root.glob(f"{site}*.dist-info/direct_url.json")]:
        content = path.read_bytes()
        repointed = re.sub(rb"(?m)(^|file://)" + re.escape(mounted), rb"\1" + own, content)
        if repointed != content:
            path.write_bytes(repointed)
    (root / WorkerWorkspace.VENV_SENTINEL).unlink(missing_ok=True)


def install_environment(token, root, git_url):
    """Repository-scoped auth for owned infrastructure fetch/readback/push only."""
    env = product_environment(root)
    authorization = _git_auth_env(token)["GIT_CONFIG_VALUE_0"]
    owned = git_url.removesuffix(".git").rstrip("/")
    # Product credentials belong to this repository. Published kit/catalog
    # fetches in child commands must remain anonymous.
    env.update(
        {
            "GIT_CONFIG_COUNT": "3",
            "GIT_CONFIG_KEY_1": f"http.{owned}/.extraheader",
            "GIT_CONFIG_KEY_2": f"http.{owned}.git/.extraheader",
            "GIT_CONFIG_VALUE_1": authorization,
            "GIT_CONFIG_VALUE_2": authorization,
        }
    )
    return env


async def run_install(msg, settings, git_url, token, fence) -> InstallResult:  # noqa: C901, PLR0912, PLR0915  # fixed stages share a workspace lease and retained head
    root = _workspace_path(settings.workspace_base_path, msg.repository_id)
    if not (root / ".git").is_dir():
        raise InstallExecutionError(
            "preflight", "workspace_unowned: ensure the owned repository first"
        )
    if not re.fullmatch(r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?:\.git)?", git_url):
        raise InstallExecutionError(
            "preflight", "repository_unowned: expected an owned GitHub HTTPS URL"
        )
    if msg.install.catalog is None:
        raise InstallExecutionError(
            "preflight", "catalog_unpinned: the install names no catalog commit; replan it"
        )
    env = product_environment(root)
    git_env = install_environment(token, root, git_url)
    try:
        lock = acquire_install_workspace_lock(root)
    except BlockingIOError as error:
        raise InstallExecutionError(
            "preflight", "branch_writer_live: workspace lock is held"
        ) from error
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
        selected_env = command_env if command_env is not None else env
        if args[0] == "git":
            if args[1] in {"fetch", "ls-remote", "push"}:
                selected_env = git_env
            args = ["git", "-c", "core.hooksPath=/dev/null", *args[1:]]
        try:
            rc, out, err = await _run_cmd(
                args, cwd=root, env=selected_env, timeout=COMMAND_TIMEOUT, kill_process_group=True
            )
        except TimeoutError as error:
            raise InstallExecutionError(
                stage, f"timeout: {shlex.join(args)} ran over {COMMAND_TIMEOUT} s", head, base
            ) from error
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
        reclaim_worker_venvs(root)
        payload = msg.install.model_dump_json()
        probe = str(Path(__file__).with_name("install_probe.py"))
        python = str(root / ".venv/bin/python")
        _, preflight = await command([python, "-I", probe, "preflight", payload, msg.template_ref])
        json.loads(preflight)
        # Each argv is platform-owned. No command, artifact, source override or
        # product path comes from task prose. The product CLI owns all mutation, and
        # it resolves every component from the payload's reviewed catalog commit, never
        # from the kit's floating default branch.
        kit = str(root / ".venv/bin/kit")
        catalog = ["--catalog-source", msg.install.catalog.repository]
        catalog += ["--catalog-ref", msg.install.catalog.commit]
        stage = "package"
        await command([kit, "add", msg.install.package.name, *catalog])
        stage = "library"
        for library in msg.install.libraries:
            await command([kit, "add", library.name, *catalog])
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
        await command(["make", "tests", f"REDIS_URL={UNIT_LEG_REDIS_URL}"])
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
        push_args = [
            "git",
            "-c",
            "core.hooksPath=/dev/null",
            "push",
            "origin",
            f"HEAD:refs/heads/{branch}",
        ]
        rc, out, err = await _run_cmd(
            push_args,
            cwd=root,
            env=git_env,
            timeout=COMMAND_TIMEOUT,
            kill_process_group=True,
        )
        stages.append(
            {
                "stage": stage,
                "argv": push_args,
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
