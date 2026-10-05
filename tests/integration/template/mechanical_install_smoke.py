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
from src.kit_catalog import KitCatalogReader, catalog_url
from src.catalog_install import plan_install_payload
async def main():
    source='https://raw.githubusercontent.com/vladmesh/codegen-product-kit'
    snapshot=await KitCatalogReader(catalog_url(source, 'HEAD'), component_source=source).read()
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


async def prove(output):
    output.mkdir(parents=True, exist_ok=True)
    artifact = {
        "candidate_sha": run(["git", "rev-parse", "HEAD"], ROOT),
        "fence_commands": [],
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
            run(["make", "setup"], product)
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
            notes = product / "services/tg_bot/src/handlers/notes.py"
            notes.parent.mkdir(parents=True)
            notes.write_text(
                '"""Owned notes customization retained through installation."""\n'
                'def note(text: str) -> str:\n    return "Saved: " + text\n'
            )
            artifact["notes_sha256"] = hashlib.sha256(notes.read_bytes()).hexdigest()
            remote = base / "remote.git"
            run(["git", "init", "--bare", "-q", "-b", "main", str(remote)], ROOT)
            run(["git", "init", "-q", "-b", "main"], product)
            run(["git", "add", "-A"], product)
            run(
                [
                    "git",
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
            run(["git", "push", "origin", "main"], product)
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
                # GitHub's repository transport is the one controlled edge. All
                # git branch/commit/non-force-push/readback operations are real.
                if argv == ["git", "remote", "get-url", "origin"]:
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
            assert artifact["notes_sha256"] == hashlib.sha256(notes.read_bytes()).hexdigest()
            assert (
                run(["git", "ls-remote", "origin", "refs/heads/story/ci-story"], product).split()[0]
                == result.head_sha
            )
            assert run(["git", "status", "--porcelain"], product) == ""
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
