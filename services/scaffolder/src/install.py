"""Mechanical install executor in one private attempt checkout of the exact story head.

The repository workspace is never the place an install runs. Each operation gets its own
worktree, derived from its operation id and detached at the owned remote story head (or
the scaffold base), with its own environments prepared by the product's own
`scripts/prepare-env.sh`. Whatever a worker left in the shared workspace — edits, Copier
rejections, an unpublished local story branch — is neither read nor overwritten.
A released catalog closure and a committed product-local package source share this path.
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
    InstallPreflight,
    InstallVerification,
)
from shared.contracts.dto.install_operation import InstallCommand
from shared.contracts.dto.local_install import (
    InstallTarget,
    LocalInstallVerification,
    LocalPackageInstall,
    local_verification_mismatch,
    preflight_mismatch,
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
#: A source check's interpreters, the kit's children included, leave no bytecode behind.
NO_BYTECODE = {"PYTHONDONTWRITEBYTECODE": "1"}
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")


class InstallExecutionError(RuntimeError):
    def __init__(self, stage, detail, head_sha=None, base_sha=None, preflight=None):
        self.stage, self.head_sha, self.base_sha = stage, head_sha, base_sha
        #: The kit's typed preflight, when the refusal is its answer.
        self.preflight: InstallPreflight | None = preflight
        super().__init__(detail)


@dataclass(frozen=True)
class InstallContext:
    """The API-admitted operation an attempt executes, with its saved target.

    Built by the consumer from the claimed operation; a local target must name this project
    and repository, whatever Git URL a caller supplies.
    """

    project_id: str
    repository_id: str
    story_id: str
    operation_id: str
    template_ref: str
    install: InstallTarget


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


def _environment(root, env):
    return root / (".venv" if env == "root" else f"services/{env}/.venv")


def environment_fingerprint(root):
    """The installed distributions of every product environment, to prove a read changed none."""
    found = {}
    for env in PRODUCT_ENVIRONMENTS:
        venv = _environment(root, env)
        found[env] = sorted(path.name for path in venv.glob("lib/*/site-packages/*.dist-info"))
    return found


def owned_tree(root):
    """Every entry under `root` with its kind and content: file SHA256, symlink target, dir.

    Symlinks are recorded by target and never followed, so nothing outside `root` is read.
    """
    if root.is_symlink() or not root.is_dir():
        raise RuntimeError(f"{root} is not an owned directory")

    def unreadable(error):
        raise error

    entries = {}
    for directory, names, files in os.walk(root, onerror=unreadable):
        for name in [*names, *files]:
            path = Path(directory) / name
            relative = str(path.relative_to(root))
            if path.is_symlink():
                entries[relative] = ["symlink", os.readlink(path)]
            elif path.is_dir():
                entries[relative] = ["dir"]
            else:
                entries[relative] = ["file", hashlib.sha256(path.read_bytes()).hexdigest()]
    return entries


def source_read_state(root, source, tracked):
    """What a source check must leave byte-identical: the source, every lock, every environment.

    Environment trees are compared whole, existing bytecode caches included.
    """
    trees = {"source": root / source}
    trees |= {env: _environment(root, env) for env in PRODUCT_ENVIRONMENTS}
    return {
        "locks": {
            name: hashlib.sha256((root / name).read_bytes()).hexdigest()
            for name in tracked
            if Path(name).name == "uv.lock"
        },
        "trees": {name: owned_tree(path) for name, path in trees.items()},
    }


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
    if (mismatch := preflight_mismatch(result, install)) is not None:
        raise InstallExecutionError("preflight", f"preflight_provenance_mismatch: {mismatch}")
    return result


def admitted_local_verification(
    output, install, protected, provenance=None
) -> LocalInstallVerification:
    """The source probe's evidence with this checkout's protected bytes, or a refusal.

    A readback is admitted only against the provenance it follows.
    """
    stage = "preflight" if provenance is None else "readback"
    try:
        verification = LocalInstallVerification.model_validate(
            json.loads(output) | {"protected_sha256": protected}
        )
    except (ValueError, TypeError) as error:
        detail = redact_diagnostic(error)[:600]
        raise InstallExecutionError(stage, f"local_verification_malformed: {detail}") from error
    if (mismatch := local_verification_mismatch(verification, install, provenance)) is not None:
        raise InstallExecutionError(stage, f"local_verification_mismatch: {mismatch}")
    return verification


def glue_detail(result: InstallPreflight, install) -> str:
    items = [
        f"{item.code} at {item.path or '-'}:{item.line or '-'}: {item.action}"
        for item in result.outstanding_glue(install)
    ]
    return ("glue_required: " + "; ".join(items))[:2000]


async def run_install(context, settings, git_url, token, fence) -> InstallResult:  # noqa: C901, PLR0912, PLR0915  # fixed stages share a workspace lease and retained head
    install = context.install
    local = isinstance(install, LocalPackageInstall)
    shared = _workspace_path(settings.workspace_base_path, context.repository_id)
    if not (shared / ".git").is_dir():
        raise InstallExecutionError(
            "preflight", "workspace_unowned: ensure the owned repository first"
        )
    if not re.fullmatch(r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?:\.git)?", git_url):
        raise InstallExecutionError(
            "preflight", "repository_unowned: expected an owned GitHub HTTPS URL"
        )
    if local and (
        str(install.project_id) != context.project_id
        or install.repository_id != context.repository_id
    ):
        raise InstallExecutionError(
            "preflight", "local_target_unowned: the source belongs to another project/repository"
        )
    if not local and install.catalog is None:
        raise InstallExecutionError(
            "preflight", "catalog_unpinned: the install names no catalog commit; replan it"
        )
    name, root = attempt_checkout(
        settings.workspace_base_path, context.repository_id, context.operation_id
    )
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
        state = (status, protected_files(root, tracked), environment_fingerprint(root))
        return (*state, source_read_state(root, install.path, tracked)) if local else state

    def protected_changes():
        return [
            item
            for item, digest in protected.items()
            if not (root / item).is_file()
            or hashlib.sha256((root / item).read_bytes()).hexdigest() != digest
        ]

    try:
        _, origin = await command(["git", "remote", "get-url", "origin"], cwd=shared)
        if origin.removesuffix(".git").rstrip("/") != git_url.removesuffix(".git").rstrip("/"):
            raise InstallExecutionError(
                stage, "repository_unowned: origin differs from the durable repository"
            )
        branch = f"story/{context.story_id}"
        await command(["git", "check-ref-format", "--branch", branch], cwd=shared)
        await command(["git", "fetch", "--no-tags", "origin"], cwd=shared)
        _, remote = await command(
            ["git", "ls-remote", "--heads", "origin", f"refs/heads/{branch}"], cwd=shared
        )
        remote = remote.split()[0] if remote else None
        # The exact owned remote story head, or the scaffold base: never a local branch.
        start = remote or "refs/remotes/origin/main"
        _, base = await command(["git", "rev-parse", "--verify", f"{start}^{{commit}}"], cwd=shared)
        if local and base != install.commit:
            # The source is installed only at the commit it was saved from; a moved head
            # needs a new plan, never an export of the old commit.
            raise InstallExecutionError(
                stage,
                "source_base_mismatch: the owned remote head is not the saved source commit; "
                "replan against the current head",
                None,
                base,
            )
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
        protected = before[1]
        payload = install.model_dump_json()
        probe = str(Path(__file__).with_name("install_probe.py"))
        python = str(root / ".venv/bin/python")
        kit = str(root / ".venv/bin/kit")
        check_env = env | NO_BYTECODE if local else None
        if local:
            # The committed source, read as data at the exact HEAD, before the kit reads it.
            _, probed = await command(
                [python, "-I", "-B", probe, "source-provenance", payload, context.template_ref],
                command_env=check_env,
            )
            verification = admitted_local_verification(probed, install, protected)
            await checkpoint()
            target = ["--package-source", install.path]
        else:
            # Independent provenance first: saved source, core, tooling, tags, default binding.
            _, probed = await command(
                [python, "-I", probe, "provenance", payload, context.template_ref]
            )
            json.loads(probed)
            target = ["--catalog-source", install.catalog.repository]
            target += ["--catalog-ref", install.catalog.commit]
        # Then the kit's read-only classification of this exact release on this product owns
        # every product conflict: language owners, command claims, settings.
        rc, answer = await command(
            [
                kit,
                "check-install",
                install.package.name,
                "--json",
                *target,
                "--version",
                install.package.version,
                "--product-root",
                str(root),
            ],
            command_env=check_env,
            accept=tuple(PREFLIGHT_EXIT_CODES.values()),
        )
        if await read_only_state() != before:
            raise InstallExecutionError(
                stage, "preflight_not_read_only: the product changed under preflight", head, base
            )
        result = admitted_preflight(rc, answer, install)
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
        if result.outstanding_glue(install):
            raise InstallExecutionError(
                stage, glue_detail(result, install), head, base, preflight=result
            )
        await checkpoint()
        if not local:
            # A release the kit admitted still passes the probe's own ownership refusals: a
            # retained binding, a claimed command or a settings schema is never overwritten.
            _, probed = await command(
                [python, "-I", probe, "preflight", payload, context.template_ref]
            )
            json.loads(probed)
            if await read_only_state() != before:
                raise InstallExecutionError(
                    stage,
                    "preflight_not_read_only: the product changed under preflight",
                    head,
                    base,
                )
        # Each argv is platform-owned. No command, artifact, source override or
        # product path comes from task prose. The product CLI owns all mutation; a catalog
        # component resolves from the payload's reviewed catalog commit, never from the
        # kit's floating default branch, and a local package from its saved source path.
        stage = "package"
        await command([kit, "add", install.package.name, *target])
        if not local:
            stage = "library"
            for library in install.libraries:
                await command([kit, "add", library.name, *target])
        # A local package is bound only when its measured source declares a default binding.
        if not local or verification.source.default_binding is not None:
            stage = "bind"
            await command([kit, "bind", install.package.name, "--default"])
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
        if local:
            _, readback = await command(
                [python, "-I", "-B", probe, "source-readback", payload, context.template_ref],
                command_env=check_env,
            )
        else:
            _, readback = await command(
                [python, "-I", probe, "readback", payload, context.template_ref]
            )
        if changed := protected_changes():
            raise InstallExecutionError(
                stage, "protected_files_changed: " + ", ".join(changed), head, base
            )
        if local:
            # Its protected bytes are the ones just re-read, equal to the snapshot.
            verification = admitted_local_verification(readback, install, protected, verification)
            if verification.residue:
                raise InstallExecutionError(
                    stage,
                    "source_residue: retained for review: " + ", ".join(verification.residue),
                    head,
                    base,
                )
            await checkpoint()
            evidence = verification.model_dump(mode="json")
        else:
            evidence = json.loads(readback)
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
        if local:
            _, current = await command(["git", "rev-parse", "--verify", "HEAD^{commit}"])
            _, tree = await command(["git", "rev-parse", "--verify", f"HEAD:{install.path}"])
            _, entries = await command(
                ["git", "status", "--porcelain", "--untracked-files=all", "--ignored=matching"]
                + ["--", f":(literal){install.path}"]
            )
            if current != install.commit or tree != verification.source.tree or entries:
                raise InstallExecutionError(
                    stage,
                    "source_changed: the source is not the saved commit's tree; retained for "
                    f"review: {entries[:1500]}",
                    head,
                    base,
                )
            if changed := protected_changes():
                raise InstallExecutionError(
                    stage, "protected_files_changed: " + ", ".join(changed), head, base
                )
            # Only native installation outputs are staged; the committed source stays as saved.
            await command(["git", "add", "-A", "--", ".", f":(exclude,literal){install.path}"])
        else:
            await command(["git", "add", "-A"])
        _, dirty = await command(["git", "status", "--porcelain"])
        if not dirty:
            raise InstallExecutionError(
                stage, "no_install_change: selected closure already exists", head, base
            )
        route = "package source" if local else "catalog package"
        await command(
            [
                "git",
                "-c",
                "user.name=Codegen Bot",
                "-c",
                "user.email=codegen@localhost",
                "commit",
                "-m",
                f"Install {route} {install.package.name} ({context.operation_id})",
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
