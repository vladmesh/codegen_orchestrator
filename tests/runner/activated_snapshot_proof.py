"""The kit's pinned runner proof, planned through this orchestrator's activated snapshot.

The kit's harness (`tests/runner/fresh_product.py` at the pinned kit commit) proves a fresh
backend,tg_bot product end to end: the orchestrator's real install executor, product CI and
drift, the cold environment regression, pushed and run image digests, the pinned platform's
real auth and Caddy with key negatives, the fixture reader, timer post delivery, the
coexistence reminder and the RU/EN causal probes. Those stages run here unchanged, from the
kit checkout.

The harness's own selection reads the kit's default branch (`catalog_url(source, 'HEAD')`),
which this orchestrator no longer plans from: a payload read at a moving ref names no
catalog commit and is refused (`catalog_unpinned`). This adapter replaces the catalog mode
and the order lifecycle, and says so in the evidence:

* catalog mode `activated_snapshot` — the catalog evidence is the commit in
  `shared/catalog_activation.yaml`, fetched by git at that commit and checked against its
  raw and semantic digests before and after the installs; the real remote's HEAD is
  recorded beside it, because it may differ and must not matter;
* the order — `tests/runner/production_plan.py` runs the production path against the
  orchestrator API built from this checkout, its database and Redis: a new owner's draft
  order, preview, brief, confirmation (and its replay), stored plan and the Architect's
  first planning attempt on that draft, which leaves its INSTALL tasks waiting at admission;
* the scaffold — instead of the harness's own Copier run, the scheduler's
  `trigger_scaffolds` publishes the full scaffold and the scaffolder's `process_scaffold_job`
  executes it from the Redis stream (Copier, `make setup`, push, readiness record); GitHub
  is the one controlled edge (`tests/runner/native_lifecycle.py`);
* the installs — each install the harness asks for is dispatched by the scheduler's
  `dispatch_todo_tasks` and delivered to the scaffolder's entrypoint, which claims, fences
  and runs it in its own attempt checkout; before the target package, the kit's own
  check-install classifies a staged product language-owner and command conflict, the API
  hands it to one repair task, a deterministic fixture repairs the story head, and a fresh
  operation installs on it;
* the confirmed answers — written by the production seed client, read back by QA's own
  pre-judgement check, then observed as the installed bot's behaviour (language and initial
  channels) before the harness's scenario writes any setting or subscribes anybody.
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
import tempfile
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]
CATALOG_MODE = "activated_snapshot"
CATALOG_FILE = "packages/catalog.yaml"
ACTIVATED_TRANSPORT = (
    "none: the activated commit of the real kit repository and its published tags, read by "
    "git at that commit; no fixture, no URL rewrite and no default branch"
)
#: The one controlled edge of the order lifecycle.
GITHUB_TRANSPORT = (
    "the scaffolder's GitHub App client answered by tests/runner/native_lifecycle.py and a "
    "local bare repository reached by a process-scoped insteadOf; every other transition is "
    "the production owner's"
)
GITHUB_ORG = "ci"
#: The Telegram user that observes the confirmed answers; no harness scenario uses it.
OBSERVER_USER = 424242301
GET_URL = ["git", "-c", "core.hooksPath=/dev/null", "remote", "get-url", "origin"]


def _harness(kit: Path):
    sys.path.insert(0, str(kit / "tests/runner"))
    import fresh_product  # noqa: PLC0415 - the pinned kit checkout's harness
    import support  # noqa: PLC0415

    return fresh_product, support


def build_runner(args: argparse.Namespace):  # noqa: C901, PLR0915 - one subclass, defined over the harness
    fresh_product, support = _harness(args.kit_dir.resolve())
    import native_lifecycle  # noqa: PLC0415 - beside this script
    from verify_evidence import seed_causality_problems  # noqa: PLC0415

    activation = yaml.safe_load(
        (args.orchestrator_dir / "shared/catalog_activation.yaml").read_text()
    )
    fresh_product.RELEASE_TRANSPORTS[CATALOG_MODE] = ACTIVATED_TRANSPORT

    class ActivatedSnapshotRunner(fresh_product.Runner):
        production: dict[str, Any]
        #: The orchestrator API and Redis of this proof, for the services' own clients.
        api_env: dict[str, str]
        product: Path

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
            self.evidence["lifecycle"] = {"github_transport": GITHUB_TRANSPORT, "steps": []}

        def production_plan(self) -> dict[str, Any]:
            orchestrator = self.orchestrator
            key = secrets.token_hex(16)
            self.secrets.add(key)
            port, redis_port = str(fresh_product.free_port()), str(fresh_product.free_port())
            compose = [
                "docker",
                "compose",
                "-p",
                f"{self.prefix}-orchestrator",
                "-f",
                str(orchestrator / "tests/runner/compose.orchestrator.yml"),
            ]
            env = self.clean_env(
                RUNNER_INTERNAL_API_KEY=key, RUNNER_API_PORT=port, RUNNER_REDIS_PORT=redis_port
            )
            self.api_env = {
                "API_BASE_URL": f"http://127.0.0.1:{port}",
                "INTERNAL_API_KEY": key,
                "REDIS_URL": f"redis://127.0.0.1:{redis_port}/0",
            }
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
                env=self.service_env("langgraph"),
                label="draft order, preview, brief, stored plan and first planning attempt",
            )
            production = json.loads(output.read_text())
            if production["activation"] != activation:
                raise fresh_product.ProofError("the production path ran another activation")
            if production["model_channels"]:
                raise fresh_product.ProofError("a model chose part of the stored plan")
            return production

        def service_env(self, service: str, **extra: str) -> dict[str, str]:
            orchestrator = self.orchestrator
            return self.clean_env(
                PYTHONPATH=f"{orchestrator / 'services' / service}:{orchestrator}",
                DEFAULT_AGENT_TYPE="claude",
                **self.api_env,
                **extra,
            )

        def step(self, kind: str, **record: Any) -> dict:
            entry = {"kind": kind, **record}
            self.evidence["lifecycle"]["steps"].append(entry)
            return entry

        def scheduler_tick(self, step: str) -> dict:
            output = self.work / f"tick-{len(self.evidence['lifecycle']['steps'])}-{step}.json"
            self.run(
                [
                    sys.executable,
                    str(self.orchestrator / "tests/runner/scheduler_tick.py"),
                    "--step",
                    step,
                    "--output",
                    str(output),
                ],
                cwd=self.orchestrator,
                env=self.service_env("scheduler"),
                label=f"scheduler tick: {step}",
            )
            return json.loads(output.read_text())

        def deliver(self, consumer: Any) -> dict:
            """One delivery in its own event loop, with the scaffolder's own API client."""
            from shared.redis import RedisStreamClient  # noqa: PLC0415
            import src.clients.api as scaffolder_api  # noqa: PLC0415

            async def once():
                scaffolder_api._client = None
                redis = RedisStreamClient(self.api_env["REDIS_URL"])
                await redis.connect()
                try:
                    return await native_lifecycle.deliver_one(redis, consumer)
                finally:
                    await redis.close()
                    if scaffolder_api._client is not None:
                        await scaffolder_api._client.close()
                    scaffolder_api._client = None

            return asyncio.run(once())

        def patched_delivery(self, consumer: Any, install: Any, executor: Any, fence: Any):
            """Deliver with the controlled GitHub edge, a recorded API and this executor."""
            current, live, github = (
                install.run_install,
                consumer.get_api_client,
                consumer.GitHubAppClient,
            )
            install.run_install = executor
            consumer.get_api_client = lambda: native_lifecycle.RecordedAPI(live(), fence)
            consumer.GitHubAppClient = native_lifecycle.ControlledGitHub
            try:
                return self.deliver(consumer)
            finally:
                install.run_install = current
                consumer.get_api_client = live
                consumer.GitHubAppClient = github

        def scaffolder_env(self, base: Path) -> None:
            os.environ.update(
                self.api_env | {"WORKSPACE_BASE_PATH": str(base), "GITHUB_ORG": GITHUB_ORG}
            )

        def scaffold(self, install: Any) -> Path:
            """The draft order is planned first; the native full scaffold releases it."""
            self.activated_snapshot()
            base = self.work / "workspaces"
            base.mkdir()
            remote = self.work / "remote.git"
            production = self.production
            tick = self.scheduler_tick("scaffolds")
            [published] = [
                item
                for item in tick["published"]
                if item["message"]["project_id"] == production["project_id"]
            ]
            message = published["message"]
            repository = production["repository"]["id"]
            if (
                message["mode"] != "full"
                or message["repository_id"] != repository
                or message["template_ref"] != self.sha
                or message["modules"] != "backend,tg_bot"
            ):
                raise fresh_product.ProofError(f"the scheduler published {message}")
            git_url = f"https://github.com/{GITHUB_ORG}/{message['project_name']}"
            self.scaffolder_env(base)
            from src import consumer  # noqa: PLC0415

            native_lifecycle.ControlledGitHub.remote = remote
            native_lifecycle.ControlledGitHub.calls = []
            transport = native_lifecycle.insteadof_config(
                self.work / "github-transport.gitconfig", remote, git_url
            )
            path = os.environ["PATH"]
            os.environ["GIT_CONFIG_GLOBAL"] = str(transport)
            os.environ["PATH"] = f"{Path(sys.executable).parent}:{path}"
            original = consumer.GitHubAppClient
            consumer.GitHubAppClient = native_lifecycle.ControlledGitHub
            try:
                delivered = self.deliver(consumer)
            finally:
                consumer.GitHubAppClient = original
                os.environ.pop("GIT_CONFIG_GLOBAL")
                os.environ["PATH"] = path
            if delivered["entry_id"] != published["entry_id"] or delivered["result"] != {
                "status": "success"
            }:
                raise fresh_product.ProofError(f"the full scaffold was not delivered: {delivered}")
            project, repo = self.api_read(
                f"projects/{production['project_id']}", f"repositories/{repository}"
            )
            config = project["config"]
            product = base / repository
            self.template_evidence(product)
            if (
                project["status"] != "active"
                or config.get("workspace_ready") is not True
                or config["service_template"]["commit"] != str(self.evidence["template"]["commit"])
                or config["service_template"]["requested_ref"] != self.sha
                or repo["git_url"] != git_url
            ):
                raise fresh_product.ProofError(f"the scaffold readiness is not recorded: {project}")
            # From here the workspace's origin is the same bare remote; installs answer the
            # owned URL through the declared transport, as the harness's own product did.
            self.git("remote", "set-url", "origin", str(remote), cwd=product, label="origin")
            self.step(
                "full_scaffold",
                tick={"entry_id": published["entry_id"], "count": tick["count"]},
                delivery={"entry_id": delivered["entry_id"], "result": delivered["result"]},
                message={key: message[key] for key in ("project_id", "repository_id", "mode")},
                project={
                    "status": project["status"],
                    "workspace_ready": config["workspace_ready"],
                    "service_template": config["service_template"],
                },
                repository={"id": repository, "git_url": repo["git_url"]},
                remote_main=self.git(
                    "rev-parse", "refs/heads/main", cwd=remote, label="scaffold head"
                ),
                github_calls=list(native_lifecycle.ControlledGitHub.calls),
            )
            self.product, self.git_url = product, git_url
            install.run_install = self.owned_executor(install)
            return product

        def api_read(self, *paths: str) -> list[dict]:
            from shared.clients.internal_api import InternalAPIClient  # noqa: PLC0415

            async def read():
                api = InternalAPIClient(self.api_env["API_BASE_URL"])
                try:
                    return [(await api.get_raw(path)).json() for path in paths]
                finally:
                    await api.close()

            return asyncio.run(read())

        def api_transition(self, task_id: str, *statuses: str) -> None:
            from shared.clients.internal_api import InternalAPIClient  # noqa: PLC0415

            async def walk():
                api = InternalAPIClient(self.api_env["API_BASE_URL"])
                try:
                    for status in statuses:
                        answer = await api.post_raw(
                            f"tasks/{task_id}/transition",
                            params={"to_status": status},
                            json={"actor": "runner-glue-fixture", "details": {}},
                        )
                        if answer.status_code != 200:
                            raise fresh_product.ProofError(f"{task_id} -> {status}: {answer.text}")
                finally:
                    await api.close()

            asyncio.run(walk())

        def template_evidence(self, product: Path) -> None:
            """The same checks the harness's own scaffold makes of its Copier render."""
            answers = yaml.safe_load((product / ".copier-answers.yml").read_text())
            resolved = self.git(
                "rev-parse", f"{answers['_commit']}^{{commit}}", cwd=self.kit, label="template"
            )
            dependencies = (product / "pyproject.toml").read_text()
            if resolved != self.sha or f"codegen-product-kit.git@{self.sha}" not in dependencies:
                raise fresh_product.ProofError(
                    "Copier answers or tooling requirement do not name the candidate"
                )
            self.evidence["template"] = {
                "source": answers["_src_path"],
                "commit": answers["_commit"],
                "resolved_sha": resolved,
                "modules": answers["modules"],
                "tooling_requirement": (
                    f"codegen-kit-tooling @ git+{fresh_product.KIT_REPOSITORY}@{self.sha}"
                ),
                "scaffolded_by": "scaffolder.src.consumer.process_scaffold_job (mode full)",
            }

        def owned_executor(self, install: Any):
            """The harness's executor call, run as the order's real dispatched operation.

            The harness keeps its transport, probe records and evidence. The call itself is
            the scheduler's dispatch tick and the scaffolder's delivery of that entry; its
            consumer claims, fences and runs `run_install`, whose result is handed back.
            """
            executor = install.run_install
            runner = self

            async def owned(message, settings, git_url, token, fence):
                name = message.install.package.name
                harness_transport = install._run_cmd

                async def transport(argv, **kwargs):
                    if argv == GET_URL:
                        return 0, runner.git_url + "\n", ""
                    return await harness_transport(argv, **kwargs)

                install._run_cmd = transport
                try:
                    if name == fresh_product.PACKAGE:
                        await asyncio.to_thread(
                            runner.native_conflict, name, install, executor, fence
                        )
                    result, record = await asyncio.to_thread(
                        runner.native_install, name, install, executor, fence
                    )
                finally:
                    install._run_cmd = harness_transport
                runner.evidence.setdefault("operations", {})[name] = record
                runner.stand_witness(name, result, record)
                runner.follow_story(record["story_id"], result.head_sha, install)
                return result

            return owned

        def stand_witness(self, name: str, result: Any, record: dict) -> None:
            """The final stand's witness, run on this operation's actual `run_install` result.

            Its inputs are retained first: the executor's stages (redacted as the harness
            redacts its own copy), beside the operation's persisted typed preflight and the
            admitted closure already in the evidence; then its disposition, so a refusal keeps
            the observed stage, command and return code.
            """
            from tests.live.install_witness import (  # noqa: PLC0415 - on the harness path
                WitnessRefused,
                check_execution,
            )

            retained = record["stand_witness"] = {
                "stages": [
                    {
                        "stage": item["stage"],
                        "argv": [self.redact(str(arg)) for arg in item["argv"]],
                        "returncode": item["returncode"],
                    }
                    for item in result.stages
                ]
            }
            try:
                retained["disposition"] = check_execution(
                    retained["stages"],
                    preflight=record["preflight"],
                    install=self.production["install_tasks"][name]["install"],
                    operation_id=record["operation_id"],
                    story_id=record["story_id"],
                    checkout=record["checkout"],
                    base_sha=record["base_sha"],
                )
            except WitnessRefused as refusal:
                retained["disposition"] = refusal.disposition()
                raise fresh_product.ProofError(
                    f"{name}: the stand witness refused the native install: {refusal}"
                ) from refusal

        def delivered_install(self, name: str, install: Any, executor: Any, fence: Any):
            """One dispatch tick and its delivery; the executor result when it ran."""
            from src import consumer  # noqa: PLC0415

            task_id = self.production["install_tasks"][name]["task_id"]
            tick = self.scheduler_tick("installs")
            published = [
                item for item in tick["published"] if item["message"].get("task_id") == task_id
            ]
            if len(published) != 1 or published[0]["message"]["mode"] != "install":
                raise fresh_product.ProofError(f"{name}: dispatch published {tick}")
            captured: dict[str, Any] = {}

            async def execute(*arguments):
                captured["result"] = await executor(*arguments)
                return captured["result"]

            delivered = self.patched_delivery(consumer, install, execute, fence)
            if delivered["entry_id"] != published[0]["entry_id"]:
                raise fresh_product.ProofError(f"{name}: another entry was delivered")
            return published[0], delivered, captured.get("result")

        def native_install(self, name: str, install: Any, executor: Any, fence: Any):
            published, delivered, result = self.delivered_install(name, install, executor, fence)
            message = published["message"]
            task_id = message["task_id"]
            [task] = self.api_read(f"tasks/{task_id}")
            record = task["install_operation"]
            checkout = f"{message['repository_id']}/{message['operation_id']}"
            if (
                delivered["result"].get("status") != "success"
                or result is None
                or task["status"] != "done"
                or record["id"] != message["operation_id"]
                or record["state"] != "published"
                or record["head_sha"] != result.head_sha
                or record.get("checkout") != checkout
                or record.get("preflight", {}).get("status") not in {"mechanical", "glue"}
            ):
                raise fresh_product.ProofError(f"{name}: unsettled install {delivered} {task}")
            self.step(
                "install",
                package=name,
                tick={"entry_id": published["entry_id"], "operation_id": message["operation_id"]},
                delivery={"entry_id": delivered["entry_id"], "result": delivered["result"]},
            )
            return result, {
                "task_id": task_id,
                "operation_id": record["id"],
                "project_id": message["project_id"],
                "repository_id": message["repository_id"],
                "story_id": message["story_id"],
                "cycle_started_at": message["cycle_started_at"],
                "dispatch_entry_id": published["entry_id"],
                "delivery_entry_id": delivered["entry_id"],
                "task_status": task["status"],
                "state": record["state"],
                "checkout": record["checkout"],
                "checkout_removed": result.checkout_removed,
                "preflight": record["preflight"],
                "base_sha": record["base_sha"],
                "head_sha": record["head_sha"],
            }

        def native_conflict(self, name: str, install: Any, executor: Any, fence: Any) -> None:
            """A real product conflict, classified by the kit and handed to one repair.

            A deterministic fixture commit on the story branch retains the package's binding
            with a product-owned language key and declares a product command the package
            claims too. The kit's check-install on the dispatched attempt must answer glue
            with both, the API must hand them to one repair task, a redelivery must run
            nothing, and after the fixture's reviewed revert a fresh operation installs.
            """
            story = self.production["story_id"]
            task_id = self.production["install_tasks"][name]["task_id"]
            payload = self.production["install_tasks"][name]["install"]
            fixture = self.stage_conflict(story, payload)
            # The real executor runs: the kit classifies the staged head before any mutation.
            published, delivered, result = self.delivered_install(name, install, executor, fence)
            message = published["message"]
            [task] = self.api_read(f"tasks/{task_id}")
            repair_id = task["blocked_by_task_id"]
            repair, events = self.api_read(f"tasks/{repair_id}", f"tasks/{task_id}/events")
            notes = [
                event["details"] for event in events if "glue_repair_task_id" in event["details"]
            ]
            if len(notes) != 1 or notes[0]["glue_repair_task_id"] != repair_id:
                raise fresh_product.ProofError(f"{name}: glue settlement notes {notes}")
            settled = notes[0]["catalog_install_settlement"]
            codes = {item["code"] for item in settled["preflight"]["glue"]}
            if (
                result is not None
                or delivered["result"].get("stage") != "preflight"
                or settled["id"] != message["operation_id"]
                or settled["preflight"]["status"] != "glue"
                or not {"binding_language_owner", "command_collision"} <= codes
                or any(item["owner"] != "product" for item in settled["preflight"]["glue"])
                or task["status"] != "todo"
                or task["install_operation"] is not None
                or repair["created_by"] != "catalog_install_glue"
                or not all(code in repair["description"] for code in codes)
            ):
                raise fresh_product.ProofError(f"{name}: no typed glue handoff {settled} {repair}")
            # Redelivery of the settled operation's entry executes nothing.
            replay = self.redeliver(message, install, fence)
            if replay["result"].get("status") != "skipped":
                raise fresh_product.ProofError(f"{name}: redelivery ran {replay}")
            repaired = self.repair_conflict(story, fixture)
            self.api_transition(repair_id, "in_dev", "in_ci", "testing", "done")
            record = {
                "fixture_commit": fixture,
                "repair_commit": repaired,
                "dispatch_entry_id": published["entry_id"],
                "delivery_entry_id": delivered["entry_id"],
                "delivery_result": delivered["result"],
                "executed": result is not None,
                "operation": settled,
                "task_after": {
                    "status": task["status"],
                    "blocked_by_task_id": repair_id,
                    "install_operation": task["install_operation"],
                },
                "repair_task": {
                    key: repair[key]
                    for key in ("id", "type", "created_by", "dispatch_admitted", "story_id")
                }
                | {
                    "description": repair["description"],
                    "blocked_by_task_id": repair["blocked_by_task_id"],
                },
                "redelivery": {"entry_id": replay["entry_id"], "result": replay["result"]},
            }
            self.evidence.setdefault("conflict_handoff", {})[name] = record
            self.step("conflict_handoff", package=name, **record)

        def redeliver(self, message: dict, install: Any, fence: Any) -> dict:
            from shared.contracts.queues.scaffold import ScaffoldMessage  # noqa: PLC0415
            from shared.queues import SCAFFOLD_QUEUE  # noqa: PLC0415
            from shared.redis import RedisStreamClient  # noqa: PLC0415
            from src import consumer  # noqa: PLC0415

            async def publish():
                redis = RedisStreamClient(self.api_env["REDIS_URL"])
                await redis.connect()
                try:
                    return await redis.publish_message(
                        SCAFFOLD_QUEUE, ScaffoldMessage.model_validate(message)
                    )
                finally:
                    await redis.close()

            entry = asyncio.run(publish())

            async def refused(*_arguments):
                raise fresh_product.ProofError("a settled operation was executed again")

            delivered = self.patched_delivery(consumer, install, refused, fence)
            if delivered["entry_id"] != entry:
                raise fresh_product.ProofError("another entry was redelivered")
            return delivered

        def stage_conflict(self, story: str, payload: dict) -> str:
            """Commit the conflict fixture on the story branch the next attempt starts from."""
            remote = self.work / "remote.git"
            scratch = Path(tempfile.mkdtemp(prefix="conflict-", dir=self.work))
            branch = f"refs/heads/story/{story}"
            head = self.git("ls-remote", str(remote), branch, cwd=self.work, label="story head")
            start = head.split()[0] if head else "main"
            self.git("clone", "-q", str(remote), str(scratch), cwd=self.work, label="fixture clone")
            self.git("checkout", "-q", "--detach", start, cwd=scratch, label="fixture base")
            module, resource = payload["binding"]["resource"].split(":", 1)
            distribution = payload["package"]["distribution"]
            tag = payload["package"]["tag"]
            source = self.work / "conflict-binding"
            source.mkdir(exist_ok=True)
            self.git("init", "-q", cwd=source, label="binding source")
            self.git(
                "fetch",
                "-q",
                "--depth=1",
                fresh_product.KIT_REPOSITORY,
                f"refs/tags/{tag}",
                cwd=source,
                label="published binding tag",
            )
            default = self.run(
                [
                    "git",
                    "show",
                    f"FETCH_HEAD:packages/{distribution}/{module.replace('.', '/')}/{resource}",
                ],
                cwd=source,
                label="published default binding",
            ).stdout
            retained = default.replace("language: {key: language,", "language: {key: bot_language,")
            if retained == default:
                raise fresh_product.ProofError("the default binding has no core language line")
            bindings = scratch / "services/tg_bot/bindings"
            bindings.mkdir(parents=True, exist_ok=True)
            (bindings / f"{payload['package']['name']}.yaml").write_text(retained)
            commands = scratch / "services/tg_bot/src/commands.py"
            declaration = "COMMANDS: tuple[ProductCommand, ...] = ()"
            text = commands.read_text()
            if text.count(declaration) != 1:
                raise fresh_product.ProofError("the product commands module changed shape")
            commands.write_text(
                text.replace(
                    declaration,
                    "async def handle_runner_channel(update, context) -> None:\n"
                    "    return None\n\n\n"
                    "COMMANDS: tuple[ProductCommand, ...] = (\n"
                    '    ProductCommand("channel", handle_runner_channel),\n'
                    ")",
                )
            )
            self.git("add", "-A", cwd=scratch, label="fixture add")
            self.git(
                "-c",
                "user.name=Runner fixture",
                "-c",
                "user.email=runner-fixture@example.com",
                "commit",
                "-qm",
                "Runner fixture: product language owner and /channel claim",
                cwd=scratch,
                label="fixture commit",
            )
            self.git("push", "-q", "origin", f"HEAD:{branch}", cwd=scratch, label="fixture push")
            self.conflict_scratch = scratch
            return self.git("rev-parse", "HEAD", cwd=scratch, label="fixture head")

        def repair_conflict(self, story: str, fixture: str) -> str:
            """The deterministic stand-in for the repair worker: revert exactly the fixture."""
            scratch = self.conflict_scratch
            self.git(
                "-c",
                "user.name=Runner fixture",
                "-c",
                "user.email=runner-fixture@example.com",
                "revert",
                "--no-edit",
                fixture,
                cwd=scratch,
                label="fixture repair",
            )
            self.git(
                "push",
                "-q",
                "origin",
                f"HEAD:refs/heads/story/{story}",
                cwd=scratch,
                label="repair",
            )
            return self.git("rev-parse", "HEAD", cwd=scratch, label="repaired head")

        def follow_story(self, story: str, head: str, install: Any) -> None:
            """The harness reads its scaffolded checkout: it follows the published head.

            The executor published `story/<story id>`; the harness reads its own fixed story
            ref, which receives the same commit by a fast-forward push to the same remote.
            """
            product, remote = self.product, self.work / "remote.git"
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
            # The confirmed answers are written, read back and observed as the bot's behaviour
            # before the harness writes any setting or subscribes anybody.
            self.confirmed_settings(deployment, "seed")
            self.seed_causality(deployment)
            self.confirmed_settings(deployment, "negatives")
            super().scenario(deployment, platform_env, key)
            observed = self.evidence["seed_causality"]["channels"]["reply"]["seq"]
            negative = self.evidence["scenario"]["negative_unknown_key"]["reply"]["seq"]
            if observed >= negative:
                raise fresh_product.ProofError("the observation did not precede the scenario")

        def confirmed_settings(self, deployment: Any, mode: str) -> None:
            """The confirmed answers through the production seed client and QA's readback."""
            capability = deployment.values["SETTINGS_WRITE_CAPABILITY"]
            self.secrets.add(capability)
            output = self.work / f"confirmed-settings-{mode}.json"
            self.run(
                [
                    sys.executable,
                    str(self.orchestrator / "tests/runner/production_settings.py"),
                    "--mode",
                    mode,
                    "--story-id",
                    self.production["story_id"],
                    "--product-url",
                    f"http://127.0.0.1:{deployment.port}",
                    "--output",
                    str(output),
                ],
                cwd=self.orchestrator,
                env=self.service_env("langgraph", SETTINGS_WRITE_CAPABILITY=capability),
                label=f"confirmed settings: {mode}",
            )
            confirmed = json.loads(output.read_text())
            if mode == "seed":
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
                for round_ in ("seed", "replay"):
                    if not all(item["written"] for item in confirmed[round_]):
                        raise fresh_product.ProofError(f"{round_} not held: {confirmed}")
                if confirmed["readback"] or confirmed["replay_readback"]:
                    raise fresh_product.ProofError(f"confirmed settings not held: {confirmed}")
                self.evidence["confirmed_settings"] = confirmed
            else:
                problems = confirmed["problems"]
                if problems:
                    raise fresh_product.ProofError(f"readback negatives did not hold: {problems}")
                self.evidence["confirmed_settings"]["negatives"] = confirmed

        def seed_causality(self, deployment: Any) -> None:
            """The installed bot's behaviour caused by the saved language and channels.

            An observer nobody else uses, granted through the ordinary users API, asks the
            bot `/channel` (its empty-name reply is in the product language) and `/channels`
            (its first reply lists the channels a new user starts with). Nothing is written to
            settings and nothing is subscribed here; the module's own API confirms the list and
            then removes it, so no later post reaches the observer.
            """
            confirmed = {
                item["key"]: item["value"]
                for item in self.evidence["confirmed_settings"]["settings"]
            }
            url = f"http://127.0.0.1:{deployment.port}"
            control = self.telegram(deployment)
            fresh_product.wait_until(
                "bot polling the Bot API",
                lambda: fresh_product.http("GET", f"{control}/control/state")[1]["calls"].get(
                    "getupdates"
                ),
                timeout=180,
            )
            identity = {"channel": "telegram", "external_id": str(OBSERVER_USER)}
            grant = fresh_product.http(
                "POST",
                f"{url}/users/grant",
                identity,
                {"X-Grant-Capability": deployment.values["USERS_GRANT_CAPABILITY"]},
            )
            observation: dict[str, Any] = {
                "observer": OBSERVER_USER,
                "grant": {"status": grant[0], "body": grant[1]},
                "expected": {
                    "language": confirmed["language"],
                    "channels": confirmed["tg_channels.starting_channels"],
                },
            }
            self.evidence["seed_causality"] = observation
            for kind, text in (("language", "/channel"), ("channels", "/channels")):
                observation[kind] = self.first_reply(control, text)
            headers = {
                "X-Identity-Capability": deployment.values["USER_IDENTITY_CAPABILITY"],
                "X-User-Channel": "telegram",
                "X-User-External-Id": str(OBSERVER_USER),
            }
            listed = fresh_product.http("GET", f"{url}/tg-channels", None, headers)
            observation["module_list"] = {"status": listed[0], "body": listed[1]}
            removed = []
            for channel in confirmed["tg_channels.starting_channels"]:
                answer = fresh_product.http("DELETE", f"{url}/tg-channels/{channel}", None, headers)
                removed.append({"channel": channel, "status": answer[0]})
            after = fresh_product.http("GET", f"{url}/tg-channels", None, headers)
            observation["cleanup"] = {
                "removed": removed,
                "after": {"status": after[0], "body": after[1]},
            }
            problems = seed_causality_problems(observation, support.LANGUAGE_REPLIES)
            if problems:
                raise fresh_product.ProofError(
                    f"the confirmed answers were not behaviour: {problems}"
                )

        def first_reply(self, control: str, text: str) -> dict:
            """Queue one input from the observer and record the chat's first reply after it."""
            watermark = len(fresh_product.http("GET", f"{control}/control/state")[1]["sent"])
            status, body = fresh_product.http(
                "POST", f"{control}/control/messages", {"user_id": OBSERVER_USER, "text": text}
            )
            if status != 200:
                raise fresh_product.ProofError(f"telegram fixture refused the update: {body}")
            replies = fresh_product.wait_until(
                f"reply to {text} in the observer chat",
                lambda: support.probe_replies(
                    fresh_product.http("GET", f"{control}/control/state")[1]["sent"],
                    OBSERVER_USER,
                    watermark,
                ),
                timeout=120,
            )
            return {
                "text": text,
                "watermark": watermark,
                "update_id": body.get("update_id"),
                "reply": replies[0],
            }

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
