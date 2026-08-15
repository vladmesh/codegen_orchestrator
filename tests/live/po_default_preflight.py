"""Operator-only evidence preflight for the Production Agent Matrix.

The workflow runs this from its already-dispatched checkout, before the worker
and QA combinations. It creates two manifest-owned draft projects through the
released PO tool: one omits ``agent_type`` from the tool argument object and
one selects a different supported agent explicitly. The code records only the
facts needed to audit that boundary, then uses the ordinary live-harness cleanup
path for both project identities.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Any, Protocol
import uuid

from live_harness import OwnershipManifest, resolve_repo_root

TOOL_IDENTIFIER = "src.agents.po.tools_projects.create_project"
SUPPORTED_EXPLICIT_AGENTS = ("claude", "codex", "factory")
FIXED_TEST_COMMAND = "matrix-po-default"
_CHECKOUT_SHA_RE = re.compile(r"[0-9a-f]{40}")
_PREFLIGHT_ID_RE = re.compile(r"[a-z0-9][a-z0-9-]{2,63}")
_PROJECT_RESPONSE_RE = re.compile(r"^Project created\. ID: ([0-9a-f-]{36}), ")


class PreflightError(RuntimeError):
    """A missing or ambiguous proof is a failed matrix preflight."""


@dataclass(frozen=True)
class PreflightConfig:
    preflight_id: str
    checkout_sha: str
    test_telegram_id: str
    output_directory: Path

    def __post_init__(self) -> None:
        if not _PREFLIGHT_ID_RE.fullmatch(self.preflight_id):
            raise PreflightError("preflight identity is not a safe matrix identifier")
        if len(f"{self.preflight_id}-omitted") > 64:
            raise PreflightError("preflight identity leaves no room for a manifest run suffix")
        if not _CHECKOUT_SHA_RE.fullmatch(self.checkout_sha):
            raise PreflightError("checkout SHA is not an exact commit SHA")
        if not self.test_telegram_id.isdecimal():
            raise PreflightError("test Telegram identity must be numeric")

    @property
    def artifact_path(self) -> Path:
        return self.output_directory / f"run-evidence-po-default-{self.preflight_id}.json"


class PreflightRuntime(Protocol):
    """The narrow seam the offline contract exercises without a live stack."""

    async def require_test_user(self, telegram_id: str) -> None: ...

    def read_runtime_default(self) -> str: ...

    def write_manifest(self, manifest: OwnershipManifest) -> None: ...

    async def invoke_create_project(
        self, arguments: dict[str, str], config: dict[str, Any]
    ) -> str: ...

    async def read_project(self, project_id: str, config: dict[str, Any]) -> dict[str, Any]: ...

    async def read_repositories(self, project_id: str) -> list[dict[str, Any]]: ...

    async def notification_snapshot(
        self, *, request_ids: list[str], telegram_id: str
    ) -> dict[str, Any]: ...

    async def notification_outbox(self, project_ids: list[str]) -> dict[str, dict[str, int]]: ...

    async def cleanup(self, ctx: dict[str, Any]) -> dict[str, Any]: ...

    async def close(self) -> None: ...


class _EvidenceWriter:
    """Keep the per-preflight evidence path exclusive for its owning process."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._handle = path.open("x+", encoding="utf-8")

    def write(self, artifact: dict[str, Any]) -> None:
        self._handle.seek(0)
        self._handle.truncate()
        json.dump(artifact, self._handle, indent=2, sort_keys=True)
        self._handle.write("\n")
        self._handle.flush()
        os.fsync(self._handle.fileno())

    def close(self) -> None:
        self._handle.close()


def _creation_context(config: PreflightConfig, variant: str) -> dict[str, Any]:
    project_id = str(uuid.uuid4())
    run_id = f"{config.preflight_id}-{variant}"
    manifest = OwnershipManifest(run_id=run_id)
    # This is deliberately before the PO call. A scheduler can see a completed
    # project creation immediately, but its parent project is already in the
    # normal recovery manifest with the same initiating run identity.
    manifest.own("project", project_id, preflight_variant=variant)
    return {
        "project_id": project_id,
        "manifest": manifest,
        "preflight_variant": variant,
        "request_id": f"{config.preflight_id}-{variant}",
    }


def _runnable_config(config: PreflightConfig, ctx: dict[str, Any]) -> dict[str, Any]:
    return {
        "configurable": {
            "thread_id": ctx["request_id"],
            "telegram_chat_id": config.test_telegram_id,
            "project_creation_identity": {
                "project_id": ctx["project_id"],
                "initiating_run_id": ctx["manifest"].run_id,
            },
        }
    }


def _assert_notification_silence(snapshot: dict[str, Any]) -> None:
    expected = {"po_response_streams", "proactive_matching_entries"}
    if set(snapshot) != expected:
        raise PreflightError("notification probe is incomplete")
    response_streams = snapshot["po_response_streams"]
    if (
        not isinstance(response_streams, dict)
        or not response_streams
        or not all(
            isinstance(stream, str) and exists is False
            for stream, exists in response_streams.items()
        )
    ):
        raise PreflightError("notification probe found a PO response")
    if snapshot["proactive_matching_entries"] != 0:
        raise PreflightError("notification probe found a proactive delivery")


def _persisted_agent(project: dict[str, Any], *, project_id: str) -> str:
    if project.get("id") != project_id:
        raise PreflightError("persisted project identifier does not match PO response")
    config = project.get("config")
    agent_type = config.get("agent_type") if isinstance(config, dict) else None
    if not isinstance(agent_type, str) or not agent_type:
        raise PreflightError("persisted project has no concrete agent type")
    return agent_type


async def _create_variant(
    runtime: PreflightRuntime,
    config: PreflightConfig,
    ctx: dict[str, Any],
    *,
    arguments: dict[str, str],
    expected_agent: str,
) -> dict[str, str]:
    runnable_config = _runnable_config(config, ctx)
    response = await runtime.invoke_create_project(arguments, runnable_config)
    if not isinstance(response, str):
        raise PreflightError("PO tool returned a non-text response")
    response_match = _PROJECT_RESPONSE_RE.match(response)
    if response_match is None or response_match.group(1) != ctx["project_id"]:
        raise PreflightError("PO response does not identify the pre-registered project")

    project = await runtime.read_project(ctx["project_id"], runnable_config)
    persisted_agent = _persisted_agent(project, project_id=ctx["project_id"])
    if persisted_agent != expected_agent:
        raise PreflightError("persisted agent differs from the requested matrix assertion")
    if project.get("initiating_run_id") != ctx["manifest"].run_id:
        raise PreflightError("persisted project lost its manifest run identity")

    repositories = await runtime.read_repositories(ctx["project_id"])
    if len(repositories) != 1:
        raise PreflightError("PO project did not create exactly one repository")
    repository_id = repositories[0].get("id")
    if not isinstance(repository_id, str) or not repository_id:
        raise PreflightError("PO repository has no identifier")
    if str(repositories[0].get("project_id")) != ctx["project_id"]:
        raise PreflightError("PO repository belongs to another project")
    ctx["repo_id"] = repository_id
    ctx["manifest"].own("repository", repository_id, project_id=ctx["project_id"])
    runtime.write_manifest(ctx["manifest"])

    return {
        "id": ctx["project_id"],
        "repository_id": repository_id,
        "initiating_run_id": ctx["manifest"].run_id,
        "persisted_agent_type": persisted_agent,
        "tool_identifier": TOOL_IDENTIFIER,
        "response_identifier": response_match.group(1),
        "po_request_id": ctx["request_id"],
    }


async def run_preflight(runtime: PreflightRuntime, config: PreflightConfig) -> dict[str, Any]:
    """Prove the PO default path and retain a cleanup-safe, redacted receipt."""
    writer = _EvidenceWriter(config.artifact_path)
    omitted_ctx = _creation_context(config, "omitted")
    explicit_ctx = _creation_context(config, "explicit")
    contexts = {"omitted": omitted_ctx, "explicit": explicit_ctx}
    for ctx in contexts.values():
        runtime.write_manifest(ctx["manifest"])

    artifact: dict[str, Any] = {
        "schema_version": 1,
        "status": "running",
        "preflight_id": config.preflight_id,
        "checkout_sha": config.checkout_sha,
        "test_identity": {"telegram_id": config.test_telegram_id},
        "tool_identifier": TOOL_IDENTIFIER,
        "fixed_test_command": FIXED_TEST_COMMAND,
        "projects": [],
        "cleanup": {},
    }
    writer.write(artifact)
    failure: Exception | None = None

    try:
        await runtime.require_test_user(config.test_telegram_id)
        runtime_default = runtime.read_runtime_default()
        if not isinstance(runtime_default, str) or not runtime_default:
            raise PreflightError("actual API runtime default is missing")
        artifact["runtime_default_agent_type"] = runtime_default

        request_ids = [omitted_ctx["request_id"], explicit_ctx["request_id"]]
        notification_before = await runtime.notification_snapshot(
            request_ids=request_ids, telegram_id=config.test_telegram_id
        )
        _assert_notification_silence(notification_before)

        omitted_arguments = {
            "title": FIXED_TEST_COMMAND,
            "modules": "backend",
            "description": FIXED_TEST_COMMAND,
        }
        if "agent_type" in omitted_arguments:
            raise PreflightError("omitted PO argument unexpectedly contains agent_type")
        artifact["omitted_argument_assertion"] = {
            "agent_type_present": False,
            "argument_keys": sorted(omitted_arguments),
        }
        omitted_project = await _create_variant(
            runtime,
            config,
            omitted_ctx,
            arguments=omitted_arguments,
            expected_agent=runtime_default,
        )
        artifact["projects"] = [omitted_project]

        explicit_agent = next(
            (agent for agent in SUPPORTED_EXPLICIT_AGENTS if agent != runtime_default), None
        )
        if explicit_agent is None:
            raise PreflightError("no supported explicit agent differs from the runtime default")
        explicit_project = await _create_variant(
            runtime,
            config,
            explicit_ctx,
            arguments={
                "title": "matrix-po-explicit",
                "modules": "backend",
                "description": FIXED_TEST_COMMAND,
                "agent_type": explicit_agent,
            },
            expected_agent=explicit_agent,
        )
        artifact["projects"].append(explicit_project)
        unchanged = await runtime.read_project(
            omitted_ctx["project_id"], _runnable_config(config, omitted_ctx)
        )
        if _persisted_agent(unchanged, project_id=omitted_ctx["project_id"]) != runtime_default:
            raise PreflightError("explicit PO choice rewrote the omitted-default project")

        notification_after = await runtime.notification_snapshot(
            request_ids=request_ids, telegram_id=config.test_telegram_id
        )
        _assert_notification_silence(notification_after)
        outbox = await runtime.notification_outbox(
            [omitted_ctx["project_id"], explicit_ctx["project_id"]]
        )
        expected_outbox = {
            project_id: {"run_count": 0, "owner_notification_count": 0}
            for project_id in (omitted_ctx["project_id"], explicit_ctx["project_id"])
        }
        if outbox != expected_outbox:
            raise PreflightError("notification outbox probe is ambiguous")

        artifact["notification_probes"] = {
            "before": notification_before,
            "after": notification_after,
            "owner_notification_outbox": outbox,
        }
    except Exception as exc:
        failure = exc if isinstance(exc, PreflightError) else PreflightError(type(exc).__name__)
    finally:
        for variant, ctx in contexts.items():
            try:
                artifact["cleanup"][variant] = await runtime.cleanup(ctx)
            except Exception as cleanup_exc:
                artifact["cleanup"][variant] = {
                    "status": "failed",
                    "failure_kind": type(cleanup_exc).__name__,
                }
                if failure is None:
                    failure = PreflightError("owned resource cleanup failed")
        try:
            await runtime.close()
        except Exception:
            if failure is None:
                failure = PreflightError("PO preflight client shutdown failed")
        artifact["status"] = "failed" if failure is not None else "passed"
        if failure is not None:
            artifact["failure"] = {"kind": type(failure).__name__}
        writer.write(artifact)
        writer.close()

    if failure is not None:
        raise failure
    return artifact


class _UnusedPOStreamClient:
    """create_project does not use Redis; any accidental use is a failed proof."""

    def __getattr__(self, name: str) -> Any:
        raise PreflightError(f"create_project unexpectedly used PO stream client attribute {name}")


class MatrixRuntime:
    """Production implementation backed by the matrix checkout and sidecar API."""

    def __init__(self, *, api_url: str, api_container: str) -> None:
        self._root = resolve_repo_root(Path(__file__))
        self._api_container = api_container
        if str(self._root) not in sys.path:
            sys.path.insert(0, str(self._root))
        if str(self._root / "services" / "langgraph") not in sys.path:
            sys.path.insert(0, str(self._root / "services" / "langgraph"))
        from shared.clients.internal_api import InternalAPIClient
        from src.agents.po.tools import create_project, init_po_clients

        self._api = InternalAPIClient(api_url)
        self._create_project = create_project
        self._init_po_clients = init_po_clients
        self._init_po_clients(self._api, _UnusedPOStreamClient())

    @staticmethod
    def _compose_redis(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["docker", "compose", "exec", "-T", "redis", "redis-cli", *args],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )

    async def require_test_user(self, telegram_id: str) -> None:
        response = await self._api.get_raw(f"users/by-telegram/{telegram_id}")
        if response.status_code == 404:
            raise PreflightError("matrix test user is absent")
        response.raise_for_status()

    def read_runtime_default(self) -> str:
        command = (
            "from src.config import get_settings; print(get_settings().default_agent_type.value)"
        )
        result = subprocess.run(
            ["docker", "exec", self._api_container, "python", "-c", command],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        value = result.stdout.strip()
        if result.returncode != 0 or value not in {"claude", "codex", "factory", "noop"}:
            raise PreflightError("actual API runtime default could not be read")
        return value

    def write_manifest(self, manifest: OwnershipManifest) -> None:
        manifest.write(self._root / ".live-manifests" / f"{manifest.run_id}.json")

    async def invoke_create_project(self, arguments: dict[str, str], config: dict[str, Any]) -> str:
        return await self._create_project.ainvoke(arguments, config=config)

    @staticmethod
    def _headers(config: dict[str, Any]) -> dict[str, str]:
        return {"X-Telegram-ID": str(config["configurable"]["telegram_chat_id"])}

    async def read_project(self, project_id: str, config: dict[str, Any]) -> dict[str, Any]:
        response = await self._api.get_raw(f"projects/{project_id}", headers=self._headers(config))
        response.raise_for_status()
        body = response.json()
        if not isinstance(body, dict):
            raise PreflightError("project read returned a non-object response")
        return body

    async def read_repositories(self, project_id: str) -> list[dict[str, Any]]:
        response = await self._api.get_raw("repositories/", params={"project_id": project_id})
        response.raise_for_status()
        body = response.json()
        if not isinstance(body, list) or not all(isinstance(item, dict) for item in body):
            raise PreflightError("repository read returned a non-list response")
        return body

    async def notification_snapshot(
        self, *, request_ids: list[str], telegram_id: str
    ) -> dict[str, Any]:
        response_streams: dict[str, bool] = {}
        for request_id in request_ids:
            response = self._compose_redis("--raw", "EXISTS", f"po:response:{request_id}")
            if response.returncode != 0 or response.stdout.strip() not in {"0", "1"}:
                raise PreflightError("PO response stream probe failed")
            response_streams[request_id] = response.stdout.strip() == "1"

        proactive = self._compose_redis("--json", "XRANGE", "po:proactive", "-", "+")
        if proactive.returncode != 0:
            raise PreflightError("PO proactive stream probe failed")
        try:
            entries = json.loads(proactive.stdout or "[]")
        except json.JSONDecodeError as exc:
            raise PreflightError("PO proactive stream probe was not machine-readable") from exc
        if not isinstance(entries, list):
            raise PreflightError("PO proactive stream probe was not a list")
        matching = 0
        for entry in entries:
            if not isinstance(entry, list) or len(entry) != 2 or not isinstance(entry[1], list):
                raise PreflightError("PO proactive stream probe entry was malformed")
            fields = entry[1]
            if len(fields) % 2:
                raise PreflightError("PO proactive stream probe fields were malformed")
            values = {
                str(fields[index]): str(fields[index + 1]) for index in range(0, len(fields), 2)
            }
            if values.get("telegram_chat_id") == telegram_id:
                matching += 1
        return {
            "po_response_streams": response_streams,
            "proactive_matching_entries": matching,
        }

    async def notification_outbox(self, project_ids: list[str]) -> dict[str, dict[str, int]]:
        result: dict[str, dict[str, int]] = {}
        for project_id in project_ids:
            response = await self._api.get_raw("runs/", params={"project_id": project_id})
            response.raise_for_status()
            runs = response.json()
            if not isinstance(runs, list) or not all(isinstance(run, dict) for run in runs):
                raise PreflightError("notification outbox run probe was not a list")
            owner_notifications = 0
            for run in runs:
                metadata = run.get("run_metadata")
                if isinstance(metadata, dict) and metadata.get("owner_notification") is not None:
                    owner_notifications += 1
            result[project_id] = {
                "run_count": len(runs),
                "owner_notification_count": owner_notifications,
            }
        return result

    async def cleanup(self, ctx: dict[str, Any]) -> dict[str, Any]:
        import pipeline_helpers

        async with (
            pipeline_helpers.api_client_as_internal_service() as api_internal,
            pipeline_helpers.api_client_as_unscoped_observer() as api_observer,
        ):
            await pipeline_helpers.cleanup_all(api_internal, api_observer, ctx)
        manifest_path = self._root / ".live-manifests" / f"{ctx['manifest'].run_id}.json"
        if manifest_path.exists():
            raise PreflightError("live cleanup left the preflight manifest behind")
        return {
            "status": "clean",
            "run_cleanup": ctx.get("run_cleanup", {}),
            "manifest_removed": True,
        }

    async def close(self) -> None:
        self._init_po_clients(None, None)
        await self._api.close()


def _required_environment(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise PreflightError(f"{name} is not set")
    return value


def _config_from_environment() -> PreflightConfig:
    return PreflightConfig(
        preflight_id=_required_environment("PO_DEFAULT_MATRIX_PREFLIGHT_ID"),
        checkout_sha=_required_environment("PO_DEFAULT_MATRIX_CHECKOUT_SHA"),
        test_telegram_id=_required_environment("PO_DEFAULT_MATRIX_TEST_TELEGRAM_ID"),
        output_directory=Path(_required_environment("PO_DEFAULT_MATRIX_EVIDENCE_DIRECTORY")),
    )


async def _main() -> None:
    config = _config_from_environment()
    actual_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], capture_output=True, text=True, timeout=15, check=False
    ).stdout.strip()
    if actual_sha != config.checkout_sha:
        raise PreflightError("matrix checkout SHA does not match the evidence identity")
    runtime = MatrixRuntime(
        api_url=_required_environment("PO_DEFAULT_MATRIX_API_URL"),
        api_container=_required_environment("PO_DEFAULT_MATRIX_API_CONTAINER"),
    )
    await run_preflight(runtime, config)


if __name__ == "__main__":
    asyncio.run(_main())
