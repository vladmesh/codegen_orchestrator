"""Mechanical catalog executor in one private attempt checkout of the exact story head.

The repository workspace is never the place an install runs. Each operation gets its own
worktree, derived from its operation id and detached at the owned remote story head (or
the scaffold base), with its own environments prepared by the product's own
`scripts/prepare-env.sh`. Whatever a worker left in the shared workspace — edits, Copier
rejections, an unpublished local story branch — is neither read nor overwritten.
"""

import asyncio
from dataclasses import dataclass, field
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shlex

from pydantic import ValidationError
import structlog

from shared.contracts.dto.catalog_install import (
    PREFLIGHT_EXIT_CODES,
    InstallCommand,
    InstallPreflight,
    InstallVerification,
)
from shared.diagnostics import redact_diagnostic
from shared.workspace_preservation import CATALOG_INSTALL_ATTEMPTS, acquire_install_workspace_lock
from src.scaffold import _git_auth_env, _run_cmd, _workspace_path

logger = structlog.get_logger(__name__)

#: The product Makefile exports its .env, whose ``redis://redis:6379`` is the
#: orchestrator Redis on this network; the kit's unit leg runs against a reserved
#: TLD that never resolves, so a runtime reaching for Redis fails fast.
UNIT_LEG_REDIS_URL = "redis://redis.invalid:6379"
COMMAND_TIMEOUT = 600
#: Every environment the installer and its probes execute: the kit tooling and both services.
PRODUCT_ENVIRONMENTS = ("root", "backend", "tg_bot")
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")


class InstallExecutionError(RuntimeError):
    def __init__(self, stage, detail, head_sha=None, base_sha=None, preflight=None):
        self.stage, self.head_sha, self.base_sha = stage, head_sha, base_sha
        #: The kit's typed preflight, when the refusal is its answer.
        self.preflight: InstallPreflight | None = preflight
        super().__init__(detail)


@dataclass
class InstallResult:
    head_sha: str
    base_sha: str
    evidence: dict
    protected_sha256: dict[str, str]
    stages: list[dict] = field(default_factory=list)
    #: `<repository id>/<operation id>`: the attempt checkout this operation ran in.
    checkout: str = ""
    preflight: dict | None = None
    #: Whether the successful attempt's checkout was removed after publication.
    checkout_removed: bool = False


def attempt_checkout(workspace_root, repository_id, operation_id) -> tuple[str, Path]:
    """The one checkout an operation's attempt may use, derived from its durable identity."""
    if not _NAME.fullmatch(repository_id) or not _NAME.fullmatch(operation_id):
        raise InstallExecutionError("prepare", "attempt_unowned: invalid repository/operation")
    base = Path(workspace_root).resolve() / CATALOG_INSTALL_ATTEMPTS
    path = base / repository_id / operation_id
    if path.resolve().parent.parent != base.resolve():
        raise InstallExecutionError("prepare", "attempt_unowned: checkout escapes its root")
    return f"{repository_id}/{operation_id}", path


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


def environment_fingerprint(root):
    """The installed distributions of every product environment, to prove a read changed none."""
    found = {}
    for env in PRODUCT_ENVIRONMENTS:
        venv = root / (".venv" if env == "root" else f"services/{env}/.venv")
        found[env] = sorted(path.name for path in venv.glob("lib/*/site-packages/*.dist-info"))
    return found


def product_environment(root, *trusted):
    """Product command environment: no inherited credential, trust in its own checkouts.

    `trusted` names further checkouts git must accept, such as the workspace whose
    repository an attempt worktree belongs to.
    """
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
    directories = [str(root), *(str(item) for item in trusted)]
    env["GIT_CONFIG_COUNT"] = str(len(directories))
    for index, directory in enumerate(directories):
        env[f"GIT_CONFIG_KEY_{index}"] = "safe.directory"
        env[f"GIT_CONFIG_VALUE_{index}"] = directory
    return env


def install_environment(token, root, git_url, *trusted):
    """Repository-scoped auth for owned infrastructure fetch/readback/push only."""
    env = product_environment(root, *trusted)
    authorization = _git_auth_env(token)["GIT_CONFIG_VALUE_0"]
    owned = git_url.removesuffix(".git").rstrip("/")
    count = int(env["GIT_CONFIG_COUNT"])
    # Product credentials belong to this repository. Published kit/catalog
    # fetches in child commands must remain anonymous.
    for offset, url in enumerate((owned, f"{owned}.git")):
        env[f"GIT_CONFIG_KEY_{count + offset}"] = f"http.{url}/.extraheader"
        env[f"GIT_CONFIG_VALUE_{count + offset}"] = authorization
    env["GIT_CONFIG_COUNT"] = str(count + 2)
    return env


def admitted_preflight(returncode, output, install) -> InstallPreflight:
    """The kit's typed result, or a refusal naming why it cannot be trusted."""
    try:
        result = InstallPreflight.model_validate_json(output)
    except ValidationError as error:
        detail = redact_diagnostic(error)[:600]
        raise InstallExecutionError("preflight", f"preflight_malformed: {detail}") from error
    if PREFLIGHT_EXIT_CODES[result.status] != returncode:
        raise InstallExecutionError(
            "preflight", f"preflight_exit_mismatch: {result.status} exited {returncode}"
        )
    if (mismatch := result.provenance_mismatch(install)) is not None:
        raise InstallExecutionError("preflight", f"preflight_provenance_mismatch: {mismatch}")
    return result


def glue_detail(result: InstallPreflight, install) -> str:
    items = [
        f"{item.code} at {item.path or '-'}:{item.line or '-'}: {item.action}"
        for item in result.outstanding_glue(install)
    ]
    return ("glue_required: " + "; ".join(items))[:2000]


async def run_install(msg, settings, git_url, token, fence) -> InstallResult:  # noqa: C901, PLR0912, PLR0915  # fixed stages share a workspace lease and retained head
    shared = _workspace_path(settings.workspace_base_path, msg.repository_id)
    if not (shared / ".git").is_dir():
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
    name, root = attempt_checkout(settings.workspace_base_path, msg.repository_id, msg.operation_id)
    if root.exists() or root.is_symlink():
        # One operation, one attempt: a redelivery never runs in a checkout it left.
        raise InstallExecutionError(
            "prepare", f"attempt_exists: {name} is retained evidence of an earlier attempt"
        )
    env = product_environment(root, shared)
    git_env = install_environment(token, root, git_url, shared)
    shared_git_env = install_environment(token, shared, git_url)
    try:
        lock = acquire_install_workspace_lock(shared)
    except BlockingIOError as error:
        raise InstallExecutionError(
            "preflight", "branch_writer_live: workspace lock is held"
        ) from error
    stage, base, head = "prepare", None, None
    stages = []
    verification = None
    preflight = None
    checkout = None

    async def checkpoint(action="checkpoint"):
        await fence(
            InstallCommand(
                action=action,
                stage=stage,
                base_sha=base,
                head_sha=head,
                verification=verification,
                checkout=checkout,
                preflight=preflight,
            )
        )

    async def command(args, *, command_env=None, cwd=None, accept=(0,)):
        await checkpoint()
        cwd = cwd or root
        selected_env = command_env if command_env is not None else env
        if args[0] == "git":
            if args[1] in {"fetch", "ls-remote", "push"}:
                selected_env = git_env if cwd == root else shared_git_env
            elif cwd == shared:
                selected_env = product_environment(shared)
            args = ["git", "-c", "core.hooksPath=/dev/null", *args[1:]]
        try:
            rc, out, err = await _run_cmd(
                args, cwd=cwd, env=selected_env, timeout=COMMAND_TIMEOUT, kill_process_group=True
            )
        except TimeoutError as error:
            raise InstallExecutionError(
                stage, f"timeout: {shlex.join(args)} ran over {COMMAND_TIMEOUT} s", head, base
            ) from error
        stages.append({"stage": stage, "argv": args, "returncode": rc})
        if rc not in accept:
            raise InstallExecutionError(
                stage, redact_diagnostic(err or out, secrets=(token,))[:2000], head, base
            )
        return rc, out.strip()

    async def remote_head():
        _, output = await command(["git", "ls-remote", "--heads", "origin", f"refs/heads/{branch}"])
        return output.split()[0] if output else None

    async def read_only_state():
        _, status = await command(["git", "status", "--porcelain", "--untracked-files=all"])
        return status, protected_files(root, tracked), environment_fingerprint(root)

    try:
        _, origin = await command(["git", "remote", "get-url", "origin"], cwd=shared)
        if origin.removesuffix(".git").rstrip("/") != git_url.removesuffix(".git").rstrip("/"):
            raise InstallExecutionError(
                stage, "repository_unowned: origin differs from the durable repository"
            )
        branch = f"story/{msg.story_id}"
        await command(["git", "check-ref-format", "--branch", branch], cwd=shared)
        await command(["git", "fetch", "--no-tags", "origin"], cwd=shared)
        _, remote = await command(
            ["git", "ls-remote", "--heads", "origin", f"refs/heads/{branch}"], cwd=shared
        )
        remote = remote.split()[0] if remote else None
        # The exact owned remote story head, or the scaffold base: never a local branch.
        start = remote or "refs/remotes/origin/main"
        _, base = await command(["git", "rev-parse", "--verify", f"{start}^{{commit}}"], cwd=shared)
        root.parent.mkdir(parents=True, exist_ok=True)
        await command(["git", "worktree", "add", "--detach", str(root), base], cwd=shared)
        checkout = name
        await checkpoint()
        _, tracked_text = await command(["git", "ls-files"])
        tracked = tracked_text.splitlines()
        if any(item.endswith((".rej", ".orig")) for item in tracked):
            raise InstallExecutionError(
                stage, "update_unresolved: review Copier rejection artifacts", head, base
            )
        # The product's own frozen environment preparation, in this checkout only.
        await command(["sh", "scripts/prepare-env.sh", *PRODUCT_ENVIRONMENTS])
        stage = "preflight"
        before = await read_only_state()
        payload = msg.install.model_dump_json()
        probe = str(Path(__file__).with_name("install_probe.py"))
        python = str(root / ".venv/bin/python")
        kit = str(root / ".venv/bin/kit")
        _, probed = await command([python, "-I", probe, "preflight", payload, msg.template_ref])
        json.loads(probed)
        # The kit's read-only admission of this exact release on this exact product.
        catalog = ["--catalog-source", msg.install.catalog.repository]
        catalog += ["--catalog-ref", msg.install.catalog.commit]
        rc, answer = await command(
            [
                kit,
                "check-install",
                msg.install.package.name,
                "--json",
                *catalog,
                "--version",
                msg.install.package.version,
                "--product-root",
                str(root),
            ],
            accept=tuple(PREFLIGHT_EXIT_CODES.values()),
        )
        if await read_only_state() != before:
            raise InstallExecutionError(
                stage, "preflight_not_read_only: the product changed under preflight", head, base
            )
        result = admitted_preflight(rc, answer, msg.install)
        preflight = result
        if result.status == "incompatible":
            raise InstallExecutionError(
                stage,
                f"preflight_incompatible: {result.incompatible.code}: "
                f"{result.incompatible.explanation}"[:2000],
                head,
                base,
                preflight=result,
            )
        if result.outstanding_glue(msg.install):
            raise InstallExecutionError(
                stage, glue_detail(result, msg.install), head, base, preflight=result
            )
        await checkpoint()
        protected = before[1]
        # Each argv is platform-owned. No command, artifact, source override or
        # product path comes from task prose. The product CLI owns all mutation, and
        # it resolves every component from the payload's reviewed catalog commit, never
        # from the kit's floating default branch.
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
            item
            for item, digest in protected.items()
            if not (root / item).is_file()
            or hashlib.sha256((root / item).read_bytes()).hexdigest() != digest
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
                item: found["version"] for item, found in evidence["distributions"].items()
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
        removed = await _remove_published_attempt(shared, root, name)
        return InstallResult(
            head_sha=head,
            base_sha=base,
            evidence=evidence,
            protected_sha256=protected,
            stages=stages,
            checkout=name,
            preflight=result.model_dump(mode="json"),
            checkout_removed=removed,
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


async def _remove_published_attempt(shared: Path, root: Path, name: str) -> bool:
    """Remove a published attempt's checkout, once every command of it has returned.

    Only a settled publication reaches here; any other ending retains the checkout as
    evidence. The removal is best effort: the publication is settled either way, and a
    checkout left behind is retained work the collector does not sweep.
    """
    try:
        rc, _, err = await _run_cmd(
            ["git", "-c", "core.hooksPath=/dev/null", "worktree", "remove", "--force", str(root)],
            cwd=shared,
            env=product_environment(shared, root),
            timeout=COMMAND_TIMEOUT,
            kill_process_group=True,
        )
    except Exception:
        logger.warning("catalog_install_attempt_retained", checkout=name, exc_info=True)
        return False
    if rc != 0:
        logger.warning(
            "catalog_install_attempt_retained", checkout=name, error=redact_diagnostic(err)[:500]
        )
        return False
    return True
