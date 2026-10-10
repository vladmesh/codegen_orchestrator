"""CI-only production executor proof over a genuine released notes product."""

import argparse
import asyncio
from dataclasses import asdict
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "services/scaffolder"))

from scripts.template_pin import TEMPLATE_PIN  # noqa: E402
from shared.contracts.queues.scaffold import ScaffoldMessage  # noqa: E402
from shared.diagnostics import redact_diagnostic  # noqa: E402
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
    declaration = "COMMANDS: tuple[ProductCommand, ...] = ()"
    assert source.count(declaration) == 1
    commands.write_text(
        source.replace(
            declaration,
            "from services.tg_bot.src.handlers.notes import handle_note, handle_notes\n\n"
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
