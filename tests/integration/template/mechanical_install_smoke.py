"""CI-only production executor proof over a genuine released notes product."""

import argparse
import asyncio
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import tomllib
from types import SimpleNamespace
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "services/scaffolder"))

from scripts.template_pin import TEMPLATE_PIN  # noqa: E402
from shared.contracts.dto.catalog_install import CatalogInstall  # noqa: E402
from shared.contracts.dto.install_operation import verification_refusal  # noqa: E402
from shared.contracts.dto.local_install import LocalPackageInstall  # noqa: E402
from shared.diagnostics import redact_diagnostic  # noqa: E402
from shared.workspace_preservation import CATALOG_INSTALL_ATTEMPTS  # noqa: E402
from src import install  # noqa: E402


def run(argv, cwd, env=None):
    result = subprocess.run(argv, cwd=cwd, env=env, capture_output=True, text=True, timeout=600)
    if result.returncode:
        raise RuntimeError(redact_diagnostic(result.stderr or result.stdout)[:4000])
    return result.stdout.strip()


def select_payload():
    code = """
import asyncio, sys
from pathlib import Path
from src.kit_catalog import get_kit_catalog_reader
from src.catalog_install import plan_install_payload
async def main():
    # The production reader: the activated snapshot's commit, verified against its digests.
    snapshot=await get_kit_catalog_reader().read()
    payload=plan_install_payload(snapshot, 'reminders', '3.12.0')
    Path(sys.argv[1]).write_text(payload.model_dump_json())
asyncio.run(main())
"""
    with tempfile.TemporaryDirectory(prefix="catalog-selection-") as scratch:
        destination = Path(scratch) / "payload.json"
        run(
            [sys.executable, "-c", code, str(destination)],
            ROOT,
            os.environ | {"PYTHONPATH": f"{ROOT / 'services/langgraph'}:{ROOT}"},
        )
        return json.loads(destination.read_text())


def customize_notes(product, python=None):
    """Retain notes commands declared in the owned bot application, registry regenerated.

    `python` runs the kit generator; the product's own tooling interpreter by default.
    """
    notes = product / "services/tg_bot/src/handlers/notes.py"
    notes.parent.mkdir(parents=True)
    notes.write_text(
        '"""Owned notes commands retained through catalog installation."""\n'
        "from telegram import Update\n"
        "from telegram.ext import ContextTypes\n\n"
        "NOTES: list[str] = []\n\n"
        "async def handle_note(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:\n"
        "    if update.message:\n"
        '        text = " ".join(context.args or [])\n'
        "        NOTES.append(text)\n"
        '        await update.message.reply_text("Saved: " + text)\n\n'
        "async def handle_notes(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:\n"
        "    if update.message:\n"
        '        await update.message.reply_text("\\n".join(NOTES))\n'
    )
    # The released core registers commands only through its registry: product commands
    # are declared in the owned commands module and the registry is regenerated from it.
    commands = product / "services/tg_bot/src/commands.py"
    source = commands.read_text()
    product_import = "from services.tg_bot.src.generated.commands import ProductCommand\n"
    declaration = "COMMANDS: tuple[ProductCommand, ...] = ()"
    assert source.count(product_import) == 1 and source.count(declaration) == 1
    commands.write_text(
        source.replace(
            product_import,
            product_import
            + "from services.tg_bot.src.handlers.notes import handle_note, handle_notes\n",
        ).replace(
            declaration,
            "COMMANDS: tuple[ProductCommand, ...] = (\n"
            '    ProductCommand("note", handle_note),\n'
            '    ProductCommand("notes", handle_notes),\n'
            ")",
        )
    )
    run([python or str(product / ".venv/bin/python"), "-m", "framework.generate"], product)
    test = product / "services/tg_bot/tests/unit/test_retained_notes.py"
    test.write_text(
        '"""The owned application registers and executes its retained notes commands."""\n'
        "from types import SimpleNamespace\n"
        "from unittest.mock import AsyncMock\n"
        "import pytest\n"
        "from services.tg_bot.src import main\n"
        "from services.tg_bot.src.handlers.notes import NOTES\n\n"
        "@pytest.mark.asyncio\n"
        "async def test_registered_notes_save_and_list(monkeypatch: pytest.MonkeyPatch) -> None:\n"
        '    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:synthetic-test-token")\n'
        "    app = main.build_application()\n"
        "    handlers = {command: handler for handler in app.handlers[0]\n"
        '                for command in getattr(handler, "commands", [])}\n'
        "    NOTES.clear()\n"
        "    message = SimpleNamespace(reply_text=AsyncMock())\n"
        "    update = SimpleNamespace(message=message)\n"
        '    await handlers["note"].callback(update, SimpleNamespace(args=["Keep", "notes"]))\n'
        '    message.reply_text.assert_awaited_once_with("Saved: Keep notes")\n'
        "    message.reply_text.reset_mock()\n"
        '    await handlers["notes"].callback(update, SimpleNamespace(args=[]))\n'
        '    message.reply_text.assert_awaited_once_with("Keep notes")\n'
        "    NOTES.clear()\n"
    )
    return notes


SOURCE_PATH = "packages/notes_source"
#: The kit's child interpreter writes no bytecode; existing cache files stay in every snapshot.
SOURCE_PREFLIGHT_CACHE_POLICY = {**install.NO_BYTECODE, "cache_files_compared": True}
#: Touched at the product root only when the package runs from its source tree.
IMPORT_MARKER = ".notes-source-imported"
STORY = "ci-story"
LOCAL_OPERATION = "ci-local-operation"


def tree_evidence(entries):
    canonical = json.dumps(entries, sort_keys=True).encode()
    return {
        "entries": len(entries),
        "files": sum(kind == "file" for kind, *_ in entries.values()),
        "symlinks": sum(kind == "symlink" for kind, *_ in entries.values()),
        "sha256": hashlib.sha256(canonical).hexdigest(),
    }


def write_source(product):
    """The synthetic notes-source package: one action, a real default binding, an import marker."""
    source = product / SOURCE_PATH
    module = source / "notes_source"
    (module / "bindings").mkdir(parents=True)
    (module / "package.yaml").write_text(
        "protocol_version: 1\n"
        "name: notes-source\n"
        "version: 1.0.0\n"
        'requires_core: ">=2.4,<3"\n'
        "package_dependencies: [fastapi]\n"
        "http:\n"
        "  prefix: /notes-source\n"
        "deployment:\n"
        "  modes: [in_process]\n"
        "actions:\n"
        "  - name: create\n"
        "    summary: Keep one memo for the verified caller.\n"
        "    operation: {method: POST, path: ''}\n"
        "    input:\n"
        "      type: object\n"
        "      properties:\n"
        "        text: {type: string, minLength: 1}\n"
        "      required: [text]\n"
        "      additionalProperties: false\n"
        "    output:\n"
        "      type: object\n"
        "      properties:\n"
        "        id: {type: string}\n"
        "        text: {type: string}\n"
        "      required: [id, text]\n"
        "      additionalProperties: false\n"
        "default_binding: notes_source:bindings/default.yaml\n"
    )
    (module / "bindings/default.yaml").write_text(
        "binding_version: 2\n"
        "package: notes-source\n"
        "language: {key: language, scope: product, values: [ru, en]}\n"
        "commands:\n"
        "  - kind: text_create\n"
        "    command: memo\n"
        "    help: {ru: 'Заметка: /memo текст', en: 'Keep a memo: /memo text'}\n"
        "    action: notes-source.create\n"
        "    args: {text: $text}\n"
        "    reply:\n"
        "      parts:\n"
        "        - {ru: 'Сохранено: ', en: 'Kept: '}\n"
        "        - {source: $result.text}\n"
        "    on_empty: {ru: Введите текст., en: Enter text.}\n"
        "    on_invalid_text: {ru: Неверный текст., en: Invalid text.}\n"
        "events: []\n"
    )
    (module / "__init__.py").write_text(
        "from pathlib import Path\n\n"
        "from fastapi import APIRouter\n\n"
        "root = Path(__file__).resolve().parents[3]\n"
        "if (root / '.copier-answers.yml').is_file():\n"
        f"    root.joinpath('{IMPORT_MARKER}').touch()\n\n"
        "router = APIRouter()\n\n\n"
        "@router.post('')\n"
        "async def create(body: dict[str, str]) -> dict[str, str]:\n"
        "    return {'id': '1', 'text': body['text']}\n\n\n"
        "class NotesSource:\n"
        "    router = router\n\n"
        "    async def startup(self, application: object) -> None:\n"
        "        return None\n\n"
        "    async def shutdown(self, application: object) -> None:\n"
        "        return None\n\n\n"
        "package = NotesSource()\n"
    )
    (source / "pyproject.toml").write_text(
        '[project]\nname = "codegen-kit-notes-source"\nversion = "1.0.0"\n'
        'requires-python = ">=3.11"\n'
        '[project.entry-points."codegen_kit.packages"]\nnotes-source = "notes_source:package"\n'
        '[build-system]\nrequires = ["hatchling"]\nbuild-backend = "hatchling.build"\n'
        '[tool.hatch.build.targets.wheel]\npackages = ["notes_source"]\n'
        "[tool.hatch.build.targets.wheel.force-include]\n"
        '"notes_source/package.yaml" = "notes_source/package.yaml"\n'
        '"notes_source/bindings/default.yaml" = "notes_source/bindings/default.yaml"\n'
    )
    return source


def local_target(product, payload, **changes):
    """The `LocalPackageInstall` a planner would save for the notes-source package at HEAD."""
    return LocalPackageInstall.model_validate(
        {
            "route": "package_source",
            "project_id": "00000000-0000-4000-8000-000000000001",
            "repository_id": product.name,
            "commit": run(["git", "rev-parse", "HEAD"], product),
            "path": SOURCE_PATH,
            "package": {
                "name": "notes-source",
                "distribution": "codegen-kit-notes-source",
                "version": "1.0.0",
            },
            "core_version": payload["core_version"],
            "python_version": payload["python_version"],
            "tooling_commit": payload["tooling_commit"],
        }
        | changes
    )


def product_state(product, tracked):
    """Git status, protected bytes and the source, lock and environment bytes a read keeps."""
    return {
        "git_status": run(["git", "status", "--porcelain", "--untracked-files=all"], product),
        "ignored": run(["git", "status", "--porcelain", "--ignored"], product),
        "protected": install.protected_files(product, tracked),
        **install.source_read_state(product, SOURCE_PATH, tracked),
    }


def state_changes(before, after):
    changed = sorted(key for key in before if key != "tree" and before[key] != after[key])
    changed += sorted(
        f"{name}:{entry}"
        for name, tree in before["tree"].items()
        for entry in set(tree) | set(after["tree"][name])
        if tree.get(entry) != after["tree"][name].get(entry)
    )
    return changed


def byte_evidence(before, after, changed):
    return {
        "before": {k: tree_evidence(v) for k, v in before["tree"].items()},
        "after": {k: tree_evidence(v) for k, v in after["tree"].items()},
        "locks": after["lock"],
        "protected_sha256": hashlib.sha256(
            json.dumps(after["protected"], sort_keys=True).encode()
        ).hexdigest(),
        "changed": changed[:50],
        "unchanged": not changed,
    }


def prove_source_preflight(product, payload, answers):
    """The pinned kit's `check-install --package-source` answer, admitted for a local target.

    The source lives inside the product and imports a marker on any runtime import; the kit
    must read it as data only, leaving product, lock and environment bytes as they were.
    `answers` is filled per invocation, so a failed comparison keeps its evidence.
    """
    assert not (product / "packages").exists()
    write_source(product)
    local = local_target(product, payload)
    tracked = run(["git", "ls-files"], product).splitlines()

    kit_env = install.product_environment(product) | install.NO_BYTECODE
    answers["cache_policy"] = SOURCE_PREFLIGHT_CACHE_POLICY
    answers["compared"] = ["git_status", "ignored", "lock", "protected", "source"]
    answers["compared"] += list(install.PRODUCT_ENVIRONMENTS)
    try:
        for label, version in (("admitted", "1.0.0"), ("version_mismatch", "1.0.1")):
            before = product_state(product, tracked)
            # The fixed argv the local executor runs from its owned checkout.
            argv = [str(product / ".venv/bin/kit"), "check-install", "notes-source", "--json"]
            argv += ["--package-source", SOURCE_PATH, "--version", version]
            argv += ["--product-root", str(product)]
            answer = subprocess.run(
                argv,
                cwd=product,
                env=kit_env,
                capture_output=True,
                text=True,
                timeout=120,
            )
            after = product_state(product, tracked)
            changed = state_changes(before, after)
            answers[label] = {
                "argv": argv[1:],
                "returncode": answer.returncode,
                "native": json.loads(answer.stdout),
                "bytes": byte_evidence(before, after, changed),
            }
            assert not changed, f"check-install {label} changed {changed[:20]}"
            assert not (product / IMPORT_MARKER).exists()
            result = install.admitted_preflight(answer.returncode, answer.stdout, local)
            answers[label]["outstanding_glue"] = len(result.outstanding_glue(local))
        admitted = answers["admitted"]["native"]
        assert admitted["status"] in {"mechanical", "glue"}
        assert admitted["target"]["route"] == "package_source"
        assert admitted["target"]["path"] == SOURCE_PATH
        assert answers["admitted"]["outstanding_glue"] == len(admitted["glue"])
        refused = answers["version_mismatch"]["native"]
        assert refused["incompatible"]["code"] == "package_mismatch"
        elsewhere = local.model_copy(update={"path": "packages/other"})
        refusal = None
        try:
            install.admitted_preflight(
                answers["admitted"]["returncode"], json.dumps(admitted), elsewhere
            )
        except install.InstallExecutionError as error:
            refusal = str(error)
        assert refusal == "preflight_provenance_mismatch: target.path"
        answers["other_path_refusal"] = refusal
    finally:
        shutil.rmtree(product / "packages")
    answers["package_runtime_imported"] = False


PROBE = ROOT / "services/scaffolder/src/install_probe.py"
#: `-B` keeps the probe's interpreter from writing bytecode; existing cache files are compared.
SOURCE_PROBE_CACHE_POLICY = {"interpreter_flags": ["-I", "-B"], "cache_files_compared": True}


def source_probes(product, local, answers):
    """The fixed source probe over `product`, each run proven read-only and recorded by label."""
    tracked = run(["git", "ls-files"], product).splitlines()
    env = install.product_environment(product) | install.NO_BYTECODE

    def probe(label, mode, target, refusal):
        before = product_state(product, tracked)
        argv = [str(product / ".venv/bin/python"), "-I", "-B", str(PROBE), mode]
        argv += [target.model_dump_json(), TEMPLATE_PIN.ref]
        result = subprocess.run(
            argv, cwd=product, env=env, capture_output=True, text=True, timeout=300
        )
        after = product_state(product, tracked)
        changed = state_changes(before, after)
        answers[label] = {
            "mode": mode,
            "target": target.model_dump(mode="json"),
            "returncode": result.returncode,
            "bytes": byte_evidence(before, after, changed),
            "refusal": redact_diagnostic((result.stderr.strip().splitlines() or [""])[-1]),
        }
        assert not changed, f"{mode} {label} changed {changed[:20]}"
        assert result.returncode != 0 and refusal in answers[label]["refusal"], answers[label]

    def substituted(path, content, label, mode, refusal):
        # A new inode each time: an installed file may be a hardlink into the uv cache.
        original = path.read_bytes() if path.exists() else None
        path.unlink(missing_ok=True)
        path.write_bytes(content(original))
        try:
            probe(label, mode, local, refusal)
        finally:
            path.unlink()
            if original is not None:
                path.write_bytes(original)

    return probe, substituted


def changed_bytes(original):
    return original + b"# substituted\n"


class CanonicalFence:
    """Records each command and holds the API's local rules for one `LocalPackageInstall`.

    The proof advances once, provenance to readback; the base is the saved commit.
    """

    def __init__(self, local, record):
        self.local, self.record, self.proofs = local, record, []

    async def __call__(self, command):
        self.record.append(command.model_dump(mode="json"))
        current = self.proofs[-1] if self.proofs else None
        if command.verification is not None:
            refusal = verification_refusal(current, command.verification, self.local)
            if refusal is not None:
                raise install.InstallExecutionError(command.stage, refusal)
            if command.verification != current:
                self.proofs.append(command.verification)
        if command.base_sha not in {None, self.local.commit}:
            raise install.InstallExecutionError(command.stage, "base_mismatch")
        if command.action == "publish" and (
            current is None
            or current.mode != "readback"
            or command.preflight is None
            or command.preflight.glue
        ):
            raise install.InstallExecutionError(command.stage, "readback_unverified")


async def execute(context, base, fence, boundary, before=None):
    """The production executor with only the owned GitHub origin observation controlled.

    `before(argv, cwd)` may read the attempt ahead of a command; the command itself is real.
    """
    actual_command = install._run_cmd

    async def transport(argv, **kwargs):
        operation = argv[3] if argv[0] == "git" else None
        credential_present = any(
            key.startswith("GIT_CONFIG_VALUE_") and value.startswith("Authorization: ")
            for key, value in kwargs["env"].items()
        )
        assert credential_present == (
            argv[0] == "git" and operation in {"fetch", "ls-remote", "push"}
        )
        boundary.append(
            {
                "executable": Path(argv[0]).name,
                "git_operation": operation,
                "credential_present": credential_present,
            }
        )
        # GitHub's repository transport is the one controlled edge. All
        # git branch/commit/non-force-push/readback operations are real.
        if argv == ["git", "-c", "core.hooksPath=/dev/null", "remote", "get-url", "origin"]:
            return 0, "https://github.com/ci/notes\n", ""
        if before is not None:
            before(argv, kwargs["cwd"])
        return await actual_command(argv, **kwargs)

    install._run_cmd = transport
    try:
        return await install.run_install(
            context,
            SimpleNamespace(workspace_base_path=str(base)),
            "https://github.com/ci/notes",
            "synthetic-ci-token",
            fence,
        )
    finally:
        install._run_cmd = actual_command


def published(product, head, name):
    return subprocess.run(
        ["git", "cat-file", "blob", f"{head}:{name}"],
        cwd=product,
        capture_output=True,
        check=True,
        timeout=60,
    ).stdout


async def prove_local_source(product, payload, base, answers):  # noqa: PLR0915  # one evidence sequence
    """The real executor installs a package source committed at the owned story head.

    Source refusals first read the workspace at that commit. The executor then runs the fixed
    probe, the kit's check-install, add, bind, generation, product checks and readback in its
    own attempt, under a fence holding the API's local transition, and publishes non-force.
    """
    write_source(product)
    run(["git", "add", "--", SOURCE_PATH], product)
    git = ["git", "-c", "core.hooksPath=/dev/null"]
    run(
        [*git, "-c", "user.name=CI", "-c", "user.email=ci@example.com"]
        + ["commit", "-qm", "Owned notes-source package"],
        product,
    )
    # The owned story branch carries the reviewed source commit the install is planned at.
    run([*git, "push", "origin", f"HEAD:refs/heads/story/{STORY}"], product)
    local = local_target(product, payload)
    parent = run(["git", "rev-parse", "HEAD~1"], product)
    answers["cache_policy"] = SOURCE_PROBE_CACHE_POLICY
    answers["source"] = {
        "commit": local.commit,
        "tree": run(["git", "rev-parse", f"HEAD:{SOURCE_PATH}"], product),
        "entries": run(["git", "ls-tree", "-r", "HEAD", "--", SOURCE_PATH], product).splitlines(),
    }
    probe, substituted = source_probes(product, local, answers.setdefault("refusals", {}))
    manifest = Path(SOURCE_PATH) / "notes_source/package.yaml"
    package = local.package.model_dump()
    for label, target, refusal in (
        ("wrong_commit", local_target(product, payload, commit=parent), "source_commit_mismatch"),
        ("wrong_path", local_target(product, payload, path="packages/other"), "path_untracked"),
        (
            "wrong_version",
            local_target(product, payload, package=package | {"version": "1.0.1"}),
            "package_identity_changed",
        ),
        (
            "wrong_distribution",
            local_target(product, payload, package=package | {"distribution": "codegen-kit-x"}),
            "package_identity_changed",
        ),
    ):
        probe(label, "source-provenance", target, refusal)
    substituted(
        product / manifest,
        changed_bytes,
        "tracked_source_changed",
        "source-provenance",
        "source_changed",
    )
    substituted(
        product / manifest.with_name("extra.py"),
        lambda _: b"",
        "untracked_source_input",
        "source-provenance",
        "source_uncommitted",
    )
    assert not (product / IMPORT_MARKER).exists()

    attempt = base / CATALOG_INSTALL_ATTEMPTS / product.name / LOCAL_OPERATION

    def readback_refusals(argv, cwd):
        """At the executor's own readback, its installed attempt refuses substituted bytes."""
        if not (len(argv) > 4 and argv[3] == str(PROBE) and argv[4] == "source-readback"):
            return
        root = Path(cwd)
        assert root == attempt.resolve() and not (root / IMPORT_MARKER).exists()
        answers["attempt_status"] = run(
            [*git, "status", "--porcelain", "--ignored", "--untracked-files=all"], root
        )
        probe, substituted = source_probes(root, local, answers["refusals"])
        site = run(
            [str(root / "services/backend/.venv/bin/python"), "-I", "-B", "-c"]
            + ["import site; print(site.getsitepackages()[0])"],
            root,
        )
        substituted(
            Path(site) / "notes_source/__init__.py",
            changed_bytes,
            "installed_file_changed",
            "source-readback",
            "installed_files_mismatch",
        )
        substituted(
            root / manifest,
            changed_bytes,
            "installed_source_changed",
            "source-readback",
            "source_changed",
        )

    context = install.InstallContext(
        project_id=str(local.project_id),
        repository_id=product.name,
        story_id=STORY,
        operation_id=LOCAL_OPERATION,
        template_ref=TEMPLATE_PIN.ref,
        install=local,
    )
    fence = CanonicalFence(local, answers.setdefault("fence_commands", []))
    boundary = answers.setdefault("credential_boundary", [])
    result = await execute(context, base, fence, boundary, readback_refusals)
    answers["execution"] = asdict(result)
    provenance, readback = fence.proofs
    assert (provenance.mode, readback.mode) == ("provenance", "readback")
    assert provenance.source.default_binding == "notes_source:bindings/default.yaml"
    assert result.checkout == f"{product.name}/{LOCAL_OPERATION}" and result.checkout_removed
    assert not attempt.exists()
    kit = [item["argv"][1:] for item in result.stages if item["argv"][0].endswith("/bin/kit")]
    assert kit[1:] == [
        ["add", "notes-source", "--package-source", SOURCE_PATH],
        ["bind", "notes-source", "--default"],
    ]
    remote = run([*git, "ls-remote", "origin", f"refs/heads/story/{STORY}"], product).split()[0]
    run([*git, "fetch", "--no-tags", "origin"], product)
    names = run(["git", "ls-tree", "-r", "--name-only", result.head_sha], product).splitlines()
    lock = tomllib.loads(published(product, result.head_sha, "services/backend/uv.lock").decode())
    [locked] = [item for item in lock["package"] if item["name"] == local.package.distribution]
    binding = published(product, result.head_sha, "services/tg_bot/bindings/notes-source.yaml")
    retained = {
        name: hashlib.sha256(published(product, result.head_sha, name)).hexdigest()
        for name in readback.protected_sha256
        if name in names
    }
    answers["publication"] = {
        "base": result.base_sha,
        "head": result.head_sha,
        "remote_head": remote,
        "parents": run(
            ["git", "rev-list", "--parents", "-n", "1", result.head_sha], product
        ).split()[1:],
        "source_tree": run(["git", "rev-parse", f"{result.head_sha}:{SOURCE_PATH}"], product),
        "wheel_sha256": hashlib.sha256(
            published(product, result.head_sha, readback.installed.wheel)
        ).hexdigest(),
        "lock": locked,
        "binding_sha256": hashlib.sha256(binding).hexdigest(),
        "retained_protected": len(retained),
        "import_marker_published": IMPORT_MARKER in names,
    }
    publication = answers["publication"]
    assert result.base_sha == local.commit and publication["parents"] == [local.commit]
    assert publication["remote_head"] == result.head_sha != local.commit
    assert publication["source_tree"] == answers["source"]["tree"]
    assert publication["wheel_sha256"] == readback.installed.wheel_sha256
    assert [item["hash"] for item in locked["wheels"]] == [
        f"sha256:{readback.installed.wheel_sha256}"
    ]
    assert publication["binding_sha256"] == provenance.source.binding_sha256
    assert publication["binding_sha256"] == readback.installed.binding_sha256
    assert retained == {name: readback.protected_sha256[name] for name in retained}
    assert not publication["import_marker_published"]
    refusal = None
    try:
        install.admitted_local_verification(
            readback.model_dump_json(),
            local,
            readback.protected_sha256 | {"pyproject.toml": "0" * 64},
            provenance,
        )
    except install.InstallExecutionError as error:
        refusal = str(error)
    assert refusal == "local_verification_mismatch: protected_sha256"
    answers["protected_change_refusal"] = refusal
    other = local.model_copy(update={"repository_id": "repo-other"})
    refusal = None
    try:
        install.admitted_local_verification(
            provenance.model_dump_json(), other, provenance.protected_sha256
        )
    except install.InstallExecutionError as error:
        refusal = str(error)
    assert refusal == "local_verification_mismatch: target.repository_id"
    answers["other_repository_refusal"] = refusal


async def prove(output):  # noqa: PLR0915  # retained CI evidence follows one owned product
    output.mkdir(parents=True, exist_ok=True)
    artifact = {
        "candidate_sha": run(["git", "rev-parse", "HEAD"], ROOT),
        "fence_commands": [],
        "credential_boundary": [],
        "status": "running",
    }
    try:
        with tempfile.TemporaryDirectory(prefix="mechanical-install-") as scratch:
            base = Path(scratch)
            product = base / "repo-ci"
            run(
                [
                    "copier",
                    "copy",
                    "--trust",
                    "--defaults",
                    f"--vcs-ref={TEMPLATE_PIN.ref}",
                    "--data",
                    "project_name=notes",
                    "--data",
                    "modules=backend,tg_bot",
                    TEMPLATE_PIN.source,
                    str(product),
                ],
                ROOT,
            )
            run(["git", "init", "-q", "-b", "main"], product)
            run(["make", "setup"], product)
            assert run(["git", "config", "--get", "core.hooksPath"], product) == ".githooks"
            run(
                [
                    str(product / "services/tg_bot/.venv/bin/python"),
                    "-c",
                    "import sys; from services.tg_bot.src.generated import bindings; "
                    "assert 'codegen_kit_textparse' not in sys.modules",
                ],
                product,
                os.environ | {"PYTHONPATH": f"{product}:{product / 'shared'}"},
            )
            notes = customize_notes(product)
            artifact["notes_sha256"] = hashlib.sha256(notes.read_bytes()).hexdigest()
            product_env = install.product_environment(product) | {
                "PYTHONPATH": f"{product}:{product / 'shared'}"
            }
            notes_test = [
                str(product / "services/tg_bot/.venv/bin/python"),
                "-m",
                "pytest",
                "services/tg_bot/tests/unit/test_retained_notes.py",
                "-q",
            ]
            run(notes_test, product, product_env)
            canary_marker = product / ".git/install-pre-push-canary"
            canary = product / ".githooks/pre-push"
            artifact["released_pre_push_sha256"] = hashlib.sha256(canary.read_bytes()).hexdigest()
            canary.write_text("#!/bin/sh\ntouch .git/install-pre-push-canary\nexit 91\n")
            canary.chmod(0o755)
            remote = base / "remote.git"
            run(["git", "init", "--bare", "-q", "-b", "main", str(remote)], ROOT)
            run(["git", "add", "-A"], product)
            run(
                [
                    "git",
                    "-c",
                    "core.hooksPath=/dev/null",
                    "-c",
                    "user.name=CI",
                    "-c",
                    "user.email=ci@example.com",
                    "commit",
                    "-qm",
                    "Owned notes baseline",
                ],
                product,
            )
            run(["git", "remote", "add", "origin", str(remote)], product)
            run(["git", "-c", "core.hooksPath=/dev/null", "push", "origin", "main"], product)
            plain_push = subprocess.run(
                ["git", "push", "origin", "main"],
                cwd=product,
                env=product_env,
                capture_output=True,
                text=True,
                timeout=30,
            )
            assert plain_push.returncode != 0 and canary_marker.is_file()
            artifact["plain_push_canary_returncode"] = plain_push.returncode
            canary_marker.unlink()
            payload = select_payload()
            artifact["source_preflight"] = {}
            prove_source_preflight(product, payload, artifact["source_preflight"])
            assert run(["git", "status", "--porcelain"], product) == ""
            context = install.InstallContext(
                project_id="ci-project",
                repository_id=product.name,
                story_id=STORY,
                operation_id="ci-operation",
                template_ref=TEMPLATE_PIN.ref,
                install=CatalogInstall.model_validate(payload),
            )

            async def fence(command):
                artifact["fence_commands"].append(command.model_dump(mode="json"))

            result = await execute(context, base, fence, artifact["credential_boundary"])
            artifact["execution"] = asdict(result)
            artifact["selection"] = payload
            # The executor ran in its own attempt checkout, admitted by the kit's preflight,
            # and removed it once published; the workspace was never its working tree.
            assert result.checkout == f"{product.name}/{context.operation_id}"
            assert result.checkout_removed
            assert not (base / CATALOG_INSTALL_ATTEMPTS / result.checkout).exists()
            assert result.preflight["status"] in {"mechanical", "glue"}
            assert result.preflight["target"]["catalog_ref"] == payload["catalog"]["commit"]
            artifact["attempt"] = {
                "checkout": result.checkout,
                "removed_after_publication": result.checkout_removed,
                "preflight": result.preflight,
                "workspace_head_unchanged": run(["git", "rev-parse", "HEAD"], product)
                == result.base_sha,
            }
            assert artifact["attempt"]["workspace_head_unchanged"]
            assert not canary_marker.exists()
            assert run(["git", "config", "--get", "core.hooksPath"], product) == ".githooks"
            artifact["product_hooks"] = {
                "configured_path": ".githooks",
                "canary_invoked_by_executor": False,
                "plain_push_refused": True,
                "exact_remote_head": result.head_sha,
            }
            assert artifact["notes_sha256"] == hashlib.sha256(notes.read_bytes()).hexdigest()
            assert (
                run(
                    [
                        "git",
                        "-c",
                        "core.hooksPath=/dev/null",
                        "ls-remote",
                        "origin",
                        f"refs/heads/story/{STORY}",
                    ],
                    product,
                ).split()[0]
                == result.head_sha
            )
            assert run(["git", "status", "--porcelain"], product) == ""
            # This checkout observes the published story head with the product's own
            # environment preparation, as any later reader of the story would.
            run(
                [
                    "git",
                    "-c",
                    "core.hooksPath=/dev/null",
                    "checkout",
                    "-q",
                    "--detach",
                    result.head_sha,
                ],
                product,
            )
            run(["sh", "scripts/prepare-env.sh", "root", "backend", "tg_bot"], product, product_env)
            run(notes_test, product, product_env)
            artifact["retained_notes_scenarios"] = {"registered": True, "save": True, "list": True}
            # Reuse the released kit's fake-backend scenario corpus, unchanged,
            # at the tooling SHA this actual product resolved and executed.
            scenario = base / "binding_scenarios.py"
            url = (
                "https://raw.githubusercontent.com/vladmesh/codegen-product-kit/"
                f"{payload['tooling_commit']}/tests/copier/binding_scenarios.py"
            )
            with urlopen(url, timeout=30) as response:  # noqa: S310  # fixed HTTPS released-kit resource
                scenario.write_bytes(response.read())
            scenario_result = run(
                [str(product / "services/tg_bot/.venv/bin/python"), str(scenario)],
                product,
                os.environ | {"PYTHONPATH": f"{product}:{product / 'shared'}"},
            )
            artifact["scenario_source"] = {
                "url": url,
                "sha256": hashlib.sha256(scenario.read_bytes()).hexdigest(),
            }
            artifact["handler_scenarios"] = json.loads(scenario_result.splitlines()[-1])
            # A package source committed on the published catalog head installs last.
            artifact["local_source"] = {}
            await prove_local_source(product, payload, base, artifact["local_source"])
            artifact["no_engineering"] = {
                "executor": "scaffolder.install.run_install",
                "task_type": "install",
                "fence_actions": [item["action"] for item in artifact["fence_commands"]],
                "paid_run_api_calls": 0,
                "worker_publications": 0,
                "scope": "executor; durable DB/Redis exclusion is covered by service tests",
            }
            artifact["status"] = "passed"
    except Exception as error:
        artifact["status"] = "failed"
        artifact["error"] = redact_diagnostic(error)[:4000]
        raise
    finally:
        (output / "mechanical-install-result.json").write_text(
            json.dumps(artifact, indent=2) + "\n"
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, required=True)
    asyncio.run(prove(parser.parse_args().output_dir))
