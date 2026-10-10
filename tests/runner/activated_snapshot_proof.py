"""The kit's pinned runner proof, planned through this orchestrator's activated snapshot.

The kit's harness (`tests/runner/fresh_product.py` at the pinned kit commit) proves a fresh
backend,tg_bot product end to end: the orchestrator's real install executor, product CI and
drift, the cold environment regression, pushed and run image digests, the pinned platform's
real auth and Caddy with key negatives, the fixture reader, timer post delivery, the
coexistence reminder and the RU/EN causal probes. Every one of those stages runs here
unchanged, from the kit checkout.

The harness's own selection reads the kit's default branch (`catalog_url(source, 'HEAD')`),
which this orchestrator no longer plans from: a payload read at a moving ref names no
catalog commit and is refused (`catalog_unpinned`). This narrow adapter replaces exactly
two things, and says so in the evidence:

* catalog mode `activated_snapshot` — the catalog evidence is the commit in
  `shared/catalog_activation.yaml`, fetched by git at that commit and checked against its
  raw and semantic digests before and after the installs; the real remote's HEAD is
  recorded beside it, because it may differ and must not matter;
* the selection — `tests/runner/production_plan.py` runs the production path against the
  orchestrator API built from this checkout and its database: a new owner's draft order,
  preview, brief, confirmation, stored plan and the Architect's first planning attempt on
  that draft. The INSTALL task payloads read back from the API are what the harness
  installs.

It adds the order's own lifecycle around the harness's unchanged stages, and says so in the
evidence: the harness's fresh scaffold is recorded by the scaffolder's own completion
writer (`_update_project_on_success`, then ACTIVE), which is what releases the INSTALL
tasks the planning left waiting at admission; each install the harness asks for is
admitted and claimed through the API and executed by the scaffolder's install consumer
(`_process_install_mode`) in its own attempt checkout under the kit's `check-install`; and
the confirmed answers are written into the deployed product and read back through the
production settings boundary before the harness's scenario.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import sys
from types import SimpleNamespace
from typing import Any

import structlog
import yaml

ROOT = Path(__file__).resolve().parents[2]
CATALOG_MODE = "activated_snapshot"
CATALOG_FILE = "packages/catalog.yaml"
ACTIVATED_TRANSPORT = (
    "none: the activated commit of the real kit repository and its published tags, read by "
    "git at that commit; no fixture, no URL rewrite and no default branch"
)


def _harness(kit: Path):
    sys.path.insert(0, str(kit / "tests/runner"))
    import fresh_product  # noqa: PLC0415 - the pinned kit checkout's harness
    import support  # noqa: PLC0415

    return fresh_product, support


def build_runner(args: argparse.Namespace):  # noqa: C901 - one subclass, defined over the harness
    fresh_product, support = _harness(args.kit_dir.resolve())
    activation = yaml.safe_load(
        (args.orchestrator_dir / "shared/catalog_activation.yaml").read_text()
    )
    fresh_product.RELEASE_TRANSPORTS[CATALOG_MODE] = ACTIVATED_TRANSPORT

    class ActivatedSnapshotRunner(fresh_product.Runner):
        production: dict[str, Any]
        #: The orchestrator API of this proof, for the scaffolder's own clients.
        api_env: dict[str, str]

        def activated_catalog(self, label: str) -> dict[str, str]:
            """The catalog at the activated commit, read by git from the real remote."""
            target = self.work / f"activated-{label}"
            target.mkdir()
            self.git("init", "-q", cwd=target, label=f"activated catalog ({label})")
            self.git(
                "fetch",
                "-q",
                "--depth=1",
                "--no-tags",
                fresh_product.KIT_REPOSITORY,
                activation["commit"],
                cwd=target,
                label=f"fetch the activated commit ({label})",
            )
            data = self.run(
                ["git", "-c", "core.hooksPath=/dev/null", "show", f"FETCH_HEAD:{CATALOG_FILE}"],
                cwd=target,
                label=f"activated catalog bytes ({label})",
            ).stdout.encode()
            found = {
                "commit": self.git("rev-parse", "FETCH_HEAD", cwd=target, label="activated commit"),
                "catalog_sha256": hashlib.sha256(data).hexdigest(),
                "catalog_digest": support.catalog_digest(data.decode()),
            }
            expected = {key: activation[key] for key in found}
            if found != expected:
                raise fresh_product.ProofError(f"activated commit holds {found}, not {expected}")
            return found

        def activated_snapshot(self) -> None:
            catalog = self.activated_catalog("before")
            self.evidence["catalog"] = {
                "mode": CATALOG_MODE,
                "prospective": False,
                "note": (
                    "the orchestrator's activated immutable catalog snapshot "
                    "(shared/catalog_activation.yaml), not the kit's default branch"
                ),
                "ref": activation["commit"],
                "source": fresh_product.KIT_REPOSITORY,
                "activation": activation,
                "remote_head_at_start": self.remote_head_catalog("head-at-start"),
            } | catalog
            self.production = self.production_plan()
            self.evidence["production_plan"] = self.production

        def production_plan(self) -> dict[str, Any]:
            orchestrator = self.orchestrator
            key = secrets.token_hex(16)
            self.secrets.add(key)
            port = str(fresh_product.free_port())
            compose = [
                "docker",
                "compose",
                "-p",
                f"{self.prefix}-orchestrator",
                "-f",
                str(orchestrator / "tests/runner/compose.orchestrator.yml"),
            ]
            env = self.clean_env(RUNNER_INTERNAL_API_KEY=key, RUNNER_API_PORT=port)
            self.api_env = {"API_BASE_URL": f"http://127.0.0.1:{port}", "INTERNAL_API_KEY": key}
            self.cleanup_later("orchestrator API", [*compose, "down", "-v"], env)
            self.run(
                [*compose, "up", "-d", "--build", "--wait"],
                cwd=orchestrator,
                env=env,
                label="orchestrator API image, database and Redis",
            )
            output = self.work / "production-plan.json"
            self.run(
                [
                    sys.executable,
                    str(orchestrator / "tests/runner/production_plan.py"),
                    "--packages",
                    ",".join(self.packages),
                    "--initial-channels",
                    fresh_product.FIXTURE_CHANNEL,
                    "--output",
                    str(output),
                ],
                cwd=orchestrator,
                env=self.langgraph_env(),
                label="draft order, preview, brief, stored plan and first planning attempt",
            )
            production = json.loads(output.read_text())
            if production["activation"] != activation:
                raise fresh_product.ProofError("the production path ran another activation")
            if production["model_channels"]:
                raise fresh_product.ProofError("a model chose part of the stored plan")
            return production

        def langgraph_env(self, **extra: str) -> dict[str, str]:
            orchestrator = self.orchestrator
            return self.clean_env(
                PYTHONPATH=f"{orchestrator / 'services/langgraph'}:{orchestrator}",
                REDIS_URL="redis://127.0.0.1:9/0",
                DEFAULT_AGENT_TYPE="claude",
                **self.api_env,
                **extra,
            )

        def scaffold(self, install: Any) -> Path:
            # A new order is planned on its draft first; the scaffold is what releases it.
            self.activated_snapshot()
            product = super().scaffold(install)
            self.record_scaffold(product)
            install.run_install = self.owned_executor(install)
            return product

        def record_scaffold(self, product: Path) -> None:
            """The scaffolder's own completion record of the harness's fresh scaffold."""
            os.environ.update(
                self.api_env
                | {"REDIS_URL": "redis://127.0.0.1:9/0", "WORKSPACE_BASE_PATH": str(product.parent)}
            )
            from shared.contracts.queues.scaffold import ScaffoldMessage  # noqa: PLC0415
            from src.clients.api import ScaffolderAPIClient  # noqa: PLC0415
            from src.consumer import _update_project_on_success  # noqa: PLC0415
            from src.scaffold import ScaffoldResult  # noqa: PLC0415

            production = self.production
            repository = production["repository"]["id"]
            # The scaffolder keeps a repository's workspace under its id; this one is the
            # checkout the harness scaffolded and pushed.
            (product.parent / repository).symlink_to(product.name)
            message = ScaffoldMessage(
                project_id=production["project_id"],
                repository_id=repository,
                template_repo=fresh_product.TEMPLATE_SOURCE,
                template_ref=self.sha,
                project_name=fresh_product.PROJECT_NAME,
                modules="backend,tg_bot",
                mode="full",
            )
            result = ScaffoldResult(
                success=True, template_commit=str(self.evidence["template"]["commit"])
            )

            async def record():
                api = ScaffolderAPIClient()
                try:
                    await api.update_repository(repository, git_url=fresh_product.GIT_URL)
                    await _update_project_on_success(
                        message,
                        result,
                        api,
                        SimpleNamespace(workspace_base_path=str(product.parent)),
                        structlog.get_logger("runner_scaffold"),
                    )
                    await api.update_project_status(message.project_id, "active")
                    return await api.get_project(message.project_id)
                finally:
                    await api.close()

            project = asyncio.run(record())
            config = project.config or {}
            if project.status != "active" or config.get("workspace_ready") is not True:
                raise fresh_product.ProofError(f"the scaffold record did not land: {project}")
            production["scaffold"] = {
                "recorded_by": "scaffolder.src.consumer._update_project_on_success",
                "project_status": project.status,
                "workspace_ready": config["workspace_ready"],
                "service_template": config.get("service_template"),
                "git_url": fresh_product.GIT_URL,
            }

        def owned_executor(self, install: Any):
            """The harness's executor call, owned by a real admitted and claimed operation.

            The harness keeps its transport, probe records and evidence; the call itself is
            admitted by the API, claimed and fenced by the scaffolder's install consumer,
            and executed by the same `run_install` in the operation's own checkout.
            """
            from shared.contracts.dto.catalog_install import InstallCommand  # noqa: PLC0415
            from shared.contracts.dto.task import TaskDTO  # noqa: PLC0415
            from shared.contracts.queues.scaffold import ScaffoldMessage  # noqa: PLC0415
            from src import consumer  # noqa: PLC0415
            from src.clients.api import ScaffolderAPIClient  # noqa: PLC0415

            executor = install.run_install
            repository = fresh_product.GIT_URL.removeprefix("https://github.com/")
            runner = self

            async def owned(message, settings, git_url, token, fence):
                name = message.install.package.name
                task_id = runner.production["install_tasks"][name]["task_id"]
                api = ScaffolderAPIClient()
                captured: dict[str, Any] = {}

                class Recorded:
                    """Every owned command also reaches the harness's fence evidence."""

                    async def catalog_install_command(self, task, body):
                        answer = await api.catalog_install_command(task, body)
                        await fence(body)
                        return answer

                async def execute(*args):
                    captured["result"] = await executor(*args)
                    return captured["result"]

                try:
                    admitted = await api.catalog_install_command(
                        task_id, InstallCommand(action="admit")
                    )
                    if admitted.outcome != "admitted" or admitted.operation is None:
                        raise fresh_product.ProofError(f"{name} was not admitted: {admitted}")
                    if admitted.install != message.install:
                        raise fresh_product.ProofError(f"{name}: admitted another closure")
                    operation = admitted.operation
                    delivered = ScaffoldMessage(
                        project_id=str(operation.project_id),
                        repository_id=operation.repository_id,
                        template_repo=message.template_repo,
                        template_ref=message.template_ref,
                        project_name=message.project_name,
                        modules="backend,tg_bot",
                        mode="install",
                        task_id=task_id,
                        story_id=operation.story_id,
                        operation_id=operation.id,
                        cycle_started_at=operation.cycle_started_at,
                        install=admitted.install,
                    )
                    install.run_install = execute
                    try:
                        outcome = await consumer._process_install_mode(
                            delivered,
                            repository,
                            None,
                            token,
                            Recorded(),
                            settings,
                            structlog.get_logger("runner_install"),
                        )
                    finally:
                        install.run_install = owned
                    task = TaskDTO.model_validate(
                        (await api.request("GET", f"tasks/{task_id}")).json()
                    )
                finally:
                    await api.close()
                if outcome.get("status") != "success" or "result" not in captured:
                    raise fresh_product.ProofError(f"{name} install was refused: {outcome}")
                result = captured["result"]
                record = task.install_operation
                checkout = f"{operation.repository_id}/{operation.id}"
                if (
                    task.status != "done"
                    or record is None
                    or record.state != "published"
                    or record.head_sha != result.head_sha
                    or record.checkout != checkout
                    or record.preflight is None
                    or record.preflight.status == "incompatible"
                ):
                    raise fresh_product.ProofError(f"{name}: unsettled operation {record}")
                runner.evidence.setdefault("operations", {})[name] = {
                    "task_id": task_id,
                    "operation_id": operation.id,
                    "story_id": operation.story_id,
                    "task_status": task.status,
                    "state": record.state,
                    "checkout": record.checkout,
                    "checkout_removed": result.checkout_removed,
                    "preflight": record.preflight.model_dump(mode="json"),
                    "base_sha": record.base_sha,
                    "head_sha": record.head_sha,
                }
                runner.follow_story(
                    Path(settings.workspace_base_path) / fresh_product.REPOSITORY_ID,
                    operation.story_id,
                    result.head_sha,
                    install,
                )
                return result

            return owned

        def follow_story(self, product: Path, story: str, head: str, install: Any) -> None:
            """The harness reads its scaffolded checkout: it follows the published head.

            The executor published `story/<story id>`; the harness reads its own fixed story
            ref, which receives the same commit by a fast-forward push to the same remote.
            """
            remote = self.work / "remote.git"
            published = self.git(
                "ls-remote", str(remote), f"refs/heads/story/{story}", cwd=self.work, label="story"
            )
            if published.split()[0] != head:
                raise fresh_product.ProofError(f"story/{story} is not the executor's head")
            self.git(
                "push",
                "-q",
                str(remote),
                f"{head}:refs/heads/story/{fresh_product.STORY}",
                cwd=product,
                label="harness story ref follows the published story",
            )
            self.git("checkout", "-q", "--detach", head, cwd=product, label="installed head")
            self.run(
                ["sh", "scripts/prepare-env.sh", "root", "backend", "tg_bot"],
                cwd=product,
                env=install.product_environment(product),
                label="installed head environments",
            )

        def scenario(self, deployment: Any, platform_env: dict[str, str], key: str) -> None:
            self.confirmed_settings(deployment)
            super().scenario(deployment, platform_env, key)

        def confirmed_settings(self, deployment: Any) -> None:
            """The confirmed answers, written and read back through the production boundary."""
            capability = deployment.values["SETTINGS_WRITE_CAPABILITY"]
            self.secrets.add(capability)
            output = self.work / "confirmed-settings.json"
            self.run(
                [
                    sys.executable,
                    str(self.orchestrator / "tests/runner/production_settings.py"),
                    "--story-id",
                    self.production["story_id"],
                    "--product-url",
                    f"http://127.0.0.1:{deployment.port}",
                    "--output",
                    str(output),
                ],
                cwd=self.orchestrator,
                env=self.langgraph_env(SETTINGS_WRITE_CAPABILITY=capability),
                label="confirmed settings seed and QA readback",
            )
            confirmed = json.loads(output.read_text())
            channels = next(
                (
                    item["value"]
                    for item in confirmed["settings"]
                    if item["key"] == "tg_channels.starting_channels"
                ),
                None,
            )
            if channels != [fresh_product.FIXTURE_CHANNEL]:
                raise fresh_product.ProofError(f"the confirmed channels are {channels}")
            if not all(item["written"] for item in confirmed["seed"]) or confirmed["readback"]:
                raise fresh_product.ProofError(f"confirmed settings not held: {confirmed}")
            self.evidence["confirmed_settings"] = confirmed

        def select_payload(self, name: str) -> dict:
            payload = self.production["install_tasks"][name]["install"]
            if payload["tooling_commit"] != self.sha or payload["package"]["name"] != name:
                raise fresh_product.ProofError("persisted payload names another tooling/package")
            if payload["catalog"]["commit"] != activation["commit"]:
                raise fresh_product.ProofError("persisted payload names another catalog commit")
            version = payload["package"]["version"]
            if name == fresh_product.PACKAGE and version != self.args.package_version:
                raise fresh_product.ProofError(
                    f"the stored plan selected {name} {version}, "
                    f"expected {self.args.package_version}"
                )
            if payload["catalog_digest"] != self.evidence["catalog"]["catalog_digest"]:
                raise fresh_product.ProofError("the stored plan names another catalog")
            stored = {
                item["install"]["package"]["name"]: item["install"]
                for item in self.production["plan"]["capabilities"]
                if item["install"]
            }
            if stored[name] != payload:
                raise fresh_product.ProofError(f"{name}: the task is not the stored closure")
            self.evidence.setdefault("install_payloads", {})[name] = payload
            if name == fresh_product.PACKAGE:
                self.evidence["install_payload"] = payload
            return payload

        def catalog_agreement(self, payloads: dict[str, dict]) -> None:
            super().catalog_agreement(payloads)
            self.evidence["catalog"]["after_install"] = self.activated_catalog("after")

    return ActivatedSnapshotRunner


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    for name in ("kit", "orchestrator", "platform"):
        parser.add_argument(f"--{name}-dir", type=Path, required=True)
        parser.add_argument(f"--{name}-sha", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--package-version", required=True)
    parser.add_argument("--packages", required=True)
    args = parser.parse_args()
    for name in ("kit", "orchestrator", "platform"):
        if not re.fullmatch(r"[0-9a-f]{40}", getattr(args, f"{name}_sha")):
            parser.error(f"--{name}-sha must be a full commit SHA")
    args.proof_mode = "published_release"
    args.catalog_mode = CATALOG_MODE
    args.packages = tuple(args.packages.split(","))
    return build_runner(args)(args).execute()


if __name__ == "__main__":
    sys.exit(main())
