"""CI-only production executor proof over a genuine released notes product."""

import argparse
import asyncio
from dataclasses import asdict
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "services/scaffolder"))

from scripts.template_pin import TEMPLATE_PIN  # noqa: E402
from shared.contracts.dto.local_install import LocalPackageInstall  # noqa: E402
from shared.contracts.queues.scaffold import ScaffoldMessage  # noqa: E402
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
SOURCE_PREFLIGHT_CACHE_POLICY = {"PYTHONDONTWRITEBYTECODE": "1", "cache_files_compared": True}


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


def tree_evidence(entries):
    canonical = json.dumps(entries, sort_keys=True).encode()
    return {
        "entries": len(entries),
        "files": sum(kind == "file" for kind, *_ in entries.values()),
        "symlinks": sum(kind == "symlink" for kind, *_ in entries.values()),
        "sha256": hashlib.sha256(canonical).hexdigest(),
    }


def prove_source_preflight(product, payload, answers):
    """The pinned kit's `check-install --package-source` answer, admitted for a local target.

    The source lives inside the product and imports a marker on any runtime import; the kit
    must read it as data only, leaving product, lock and environment bytes as they were.
    `answers` is filled per invocation, so a failed comparison keeps its evidence.
    """
    source = product / SOURCE_PATH
    assert not (product / "packages").exists()
    module = source / "notes_source"
    module.mkdir(parents=True)
    (module / "package.yaml").write_text(
        "protocol_version: 1\n"
        "name: notes-source\n"
        "version: 1.0.0\n"
        'requires_core: ">=2,<3"\n'
        "http:\n"
        "  prefix: /notes-source\n"
        "deployment:\n"
        "  modes: [in_process]\n"
    )
    (module / "__init__.py").write_text(
        "from pathlib import Path\n"
        "Path(__file__).resolve().parents[3].joinpath('.git/notes-source-imported').touch()\n"
    )
    (source / "pyproject.toml").write_text(
        '[project]\nname = "codegen-kit-notes-source"\nversion = "1.0.0"\n'
        '[project.entry-points."codegen_kit.packages"]\nnotes-source = "notes_source:package"\n'
    )
    local = LocalPackageInstall.model_validate(
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
    )
    tracked = run(["git", "ls-files"], product).splitlines()

    owned = {
        "source": source,
        "root": product / ".venv",
        "backend": product / "services/backend/.venv",
        "tg_bot": product / "services/tg_bot/.venv",
    }

    def state():
        return {
            "git_status": run(["git", "status", "--porcelain", "--untracked-files=all"], product),
            "ignored": run(["git", "status", "--porcelain", "--ignored"], product),
            "locks": {
                name: hashlib.sha256((product / name).read_bytes()).hexdigest()
                for name in tracked
                if Path(name).name == "uv.lock"
            },
            "protected": install.protected_files(product, tracked),
            "trees": {name: owned_tree(path) for name, path in owned.items()},
        }

    kit_env = install.product_environment(product) | {
        "PYTHONDONTWRITEBYTECODE": SOURCE_PREFLIGHT_CACHE_POLICY["PYTHONDONTWRITEBYTECODE"]
    }
    answers["cache_policy"] = SOURCE_PREFLIGHT_CACHE_POLICY
    answers["compared"] = ["git_status", "ignored", "locks", "protected", *owned]
    try:
        for label, version in (("admitted", "1.0.0"), ("version_mismatch", "1.0.1")):
            before = state()
            # The fixed argv a local executor would run from its owned checkout.
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
            after = state()
            changed = sorted(key for key in before if key != "trees" and before[key] != after[key])
            changed += sorted(
                f"{name}:{entry}"
                for name, tree in before["trees"].items()
                for entry in set(tree) | set(after["trees"][name])
                if tree.get(entry) != after["trees"][name].get(entry)
            )
            answers[label] = {
                "argv": argv[1:],
                "returncode": answer.returncode,
                "native": json.loads(answer.stdout),
                "bytes": {
                    "before": {k: tree_evidence(v) for k, v in before["trees"].items()},
                    "after": {k: tree_evidence(v) for k, v in after["trees"].items()},
                    "locks": after["locks"],
                    "protected_sha256": hashlib.sha256(
                        json.dumps(after["protected"], sort_keys=True).encode()
                    ).hexdigest(),
                    "changed": changed[:50],
                    "unchanged": not changed,
                },
            }
            assert not changed, f"check-install {label} changed {changed[:20]}"
            assert not (product / ".git/notes-source-imported").exists()
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
            message = ScaffoldMessage(
                project_id="ci-project",
                repository_id=product.name,
                template_repo=TEMPLATE_PIN.source,
                template_ref=TEMPLATE_PIN.ref,
                project_name="notes",
                modules="backend,tg_bot",
                mode="install",
                task_id="ci-install",
                story_id="ci-story",
                operation_id="ci-operation",
                cycle_started_at=datetime.now(UTC),
                install=payload,
            )
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
                artifact["credential_boundary"].append(
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
                return await actual_command(argv, **kwargs)

            async def fence(command):
                artifact["fence_commands"].append(command.model_dump(mode="json"))

            install._run_cmd = transport
            try:
                result = await install.run_install(
                    message,
                    SimpleNamespace(workspace_base_path=str(base)),
                    "https://github.com/ci/notes",
                    "synthetic-ci-token",
                    fence,
                )
            finally:
                install._run_cmd = actual_command
            artifact["execution"] = asdict(result)
            artifact["selection"] = payload
            # The executor ran in its own attempt checkout, admitted by the kit's preflight,
            # and removed it once published; the workspace was never its working tree.
            assert result.checkout == f"{product.name}/{message.operation_id}"
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
                        "refs/heads/story/ci-story",
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
