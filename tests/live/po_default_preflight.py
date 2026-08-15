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
from typing import Any, Literal, Protocol
import uuid

from live_harness import OwnershipManifest, resolve_repo_root

TOOL_IDENTIFIER = "src.agents.po.tools_projects.create_project"
SUPPORTED_EXPLICIT_AGENTS = ("claude", "codex", "factory")
FIXED_TEST_COMMAND = "matrix-po-default"
_CHECKOUT_SHA_RE = re.compile(r"[0-9a-f]{40}")
_PREFLIGHT_ID_RE = re.compile(r"[a-z0-9][a-z0-9-]{2,63}")
_PROJECT_RESPONSE_RE = re.compile(r"^Project created\. ID: ([0-9a-f-]{36}), ")
_STREAM_ID_RE = re.compile(r"[0-9]+-[0-9]+")
_SENSITIVE_FAILURE_VALUE_RE = re.compile(
    r"(?i)(api[_-]?key|token|secret|password|authorization)\s*(?:=|:)\s*"
    r"(?:bearer\s+)?[^,\s]+"
)


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
        if len(f"{self.preflight_id}-explicit") > 64:
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

    async def ensure_test_user(self, telegram_id: str) -> None: ...

    def read_runtime_default(self) -> str: ...

    def write_manifest(self, manifest: OwnershipManifest) -> None: ...

    async def invoke_create_project(
        self, arguments: dict[str, str], config: dict[str, Any]
    ) -> str: ...

    async def read_project(self, project_id: str, config: dict[str, Any]) -> dict[str, Any]: ...

    async def read_repositories(self, project_id: str) -> list[dict[str, Any]]: ...

    async def notification_snapshot(
        self,
        *,
        request_ids: list[str],
        project_ids: list[str],
        telegram_id: str,
        phase: Literal["before", "after"],
        proactive_after_id: str | None,
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


def _notification_violations(
    snapshot: dict[str, Any],
    *,
    phase: Literal["before", "after"],
    project_ids: set[str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    expected = (
        {"po_response_streams", "proactive_boundary"}
        if phase == "before"
        else {"po_response_streams", "proactive_delta"}
    )
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
    if phase == "before":
        boundary = snapshot["proactive_boundary"]
        if (
            not isinstance(boundary, dict)
            or set(boundary) != {"stream_id"}
            or (
                boundary["stream_id"] is not None
                and (
                    not isinstance(boundary["stream_id"], str)
                    or _STREAM_ID_RE.fullmatch(boundary["stream_id"]) is None
                )
            )
        ):
            raise PreflightError("notification boundary probe is malformed")
        return [], []

    delta = snapshot["proactive_delta"]
    if not isinstance(delta, list):
        raise PreflightError("notification delta probe is malformed")
    owned_entries: list[dict[str, Any]] = []
    ambiguous_entries: list[dict[str, Any]] = []
    for entry in delta:
        if (
            not isinstance(entry, dict)
            or set(entry) != {"stream_id", "project_id", "project_identity"}
            or not isinstance(entry["stream_id"], str)
            or _STREAM_ID_RE.fullmatch(entry["stream_id"]) is None
            or entry["project_identity"] not in {"valid", "missing", "invalid"}
        ):
            raise PreflightError("notification delta entry is malformed")
        project_id = entry["project_id"]
        if entry["project_identity"] == "valid":
            try:
                canonical_project_id = str(uuid.UUID(str(project_id)))
            except (TypeError, ValueError) as exc:
                raise PreflightError("notification delta project identity is malformed") from exc
            if canonical_project_id != project_id:
                raise PreflightError("notification delta project identity is malformed")
            if project_id in project_ids:
                owned_entries.append(entry)
            continue
        if project_id is not None:
            raise PreflightError("notification delta project identity is malformed")
        ambiguous_entries.append(entry)
    return owned_entries, ambiguous_entries


def _register_owned_proactive_entries(
    runtime: PreflightRuntime,
    contexts: dict[str, dict[str, Any]],
    entries: list[dict[str, Any]],
) -> None:
    contexts_by_project_id = {ctx["project_id"]: ctx for ctx in contexts.values()}
    for entry in entries:
        ctx = contexts_by_project_id[entry["project_id"]]
        ctx["manifest"].own("redis_entry", entry["stream_id"], stream="po:proactive")
        runtime.write_manifest(ctx["manifest"])


def _failure_receipt(exc: Exception) -> dict[str, str]:
    message = _SENSITIVE_FAILURE_VALUE_RE.sub(r"\1=<redacted>", str(exc))
    return {"kind": type(exc).__name__, "message": message[:1000]}


async def _capture_notification_boundary(
    runtime: PreflightRuntime,
    config: PreflightConfig,
    contexts: dict[str, dict[str, Any]],
) -> tuple[dict[str, Any], list[str]]:
    request_ids = [ctx["request_id"] for ctx in contexts.values()]
    project_ids = [ctx["project_id"] for ctx in contexts.values()]
    snapshot = await runtime.notification_snapshot(
        request_ids=request_ids,
        project_ids=project_ids,
        telegram_id=config.test_telegram_id,
        phase="before",
        proactive_after_id=None,
    )
    _notification_violations(snapshot, phase="before", project_ids=set(project_ids))
    return snapshot, project_ids


async def _verify_post_creation_notification_silence(
    runtime: PreflightRuntime,
    config: PreflightConfig,
    contexts: dict[str, dict[str, Any]],
    *,
    project_ids: list[str],
    notification_before: dict[str, Any],
    artifact: dict[str, Any],
) -> None:
    request_ids = [ctx["request_id"] for ctx in contexts.values()]
    notification_after = await runtime.notification_snapshot(
        request_ids=request_ids,
        project_ids=project_ids,
        telegram_id=config.test_telegram_id,
        phase="after",
        proactive_after_id=notification_before["proactive_boundary"]["stream_id"],
    )
    artifact["notification_probes"]["after"] = notification_after
    owned_entries, ambiguous_entries = _notification_violations(
        notification_after,
        phase="after",
        project_ids=set(project_ids),
    )
    _register_owned_proactive_entries(runtime, contexts, owned_entries)
    outbox = await runtime.notification_outbox(project_ids)
    expected_outbox = {
        project_id: {"run_count": 0, "owner_notification_count": 0} for project_id in project_ids
    }
    artifact["notification_probes"]["owner_notification_outbox"] = outbox
    if outbox != expected_outbox:
        raise PreflightError("notification outbox probe is ambiguous")
    if owned_entries:
        raise PreflightError("notification probe found an owned proactive delivery")
    if ambiguous_entries:
        raise PreflightError("notification probe found an ambiguous proactive delivery")


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
    failure: PreflightError | None = None
    underlying_failure: Exception | None = None

    try:
        await runtime.ensure_test_user(config.test_telegram_id)
        runtime_default = runtime.read_runtime_default()
        if not isinstance(runtime_default, str) or not runtime_default:
            raise PreflightError("actual API runtime default is missing")
        artifact["runtime_default_agent_type"] = runtime_default

        notification_before, project_ids = await _capture_notification_boundary(
            runtime, config, contexts
        )
        artifact["notification_probes"] = {"before": notification_before}

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

        await _verify_post_creation_notification_silence(
            runtime,
            config,
            contexts,
            project_ids=project_ids,
            notification_before=notification_before,
            artifact=artifact,
        )
    except Exception as exc:
        underlying_failure = exc
        failure = (
            exc
            if isinstance(exc, PreflightError)
            else PreflightError(f"{type(exc).__name__}: {exc}")
        )
    finally:
        for variant, ctx in contexts.items():
            try:
                artifact["cleanup"][variant] = await runtime.cleanup(ctx)
            except Exception as cleanup_exc:
                artifact["cleanup"][variant] = {
                    "status": "failed",
                    "failure": _failure_receipt(cleanup_exc),
                }
                if failure is None:
                    underlying_failure = cleanup_exc
                    failure = PreflightError("owned resource cleanup failed")
        try:
            await runtime.close()
        except Exception as close_exc:
            if failure is None:
                underlying_failure = close_exc
                failure = PreflightError("PO preflight client shutdown failed")
        artifact["status"] = "failed" if failure is not None else "passed"
        if failure is not None:
            artifact["failure"] = _failure_receipt(underlying_failure or failure)
        writer.write(artifact)
        writer.close()

    if failure is not None:
        raise failure
    return artifact


class _UnusedPOStreamClient:
    """create_project does not use Redis; any accidental use is a failed proof."""

    def __getattr__(self, name: str) -> Any:
        raise PreflightError(f"create_project unexpectedly used PO stream client attribute {name}")


def load_po_tool_boundary(root: Path) -> tuple[Any, Any]:
    """Bind the released PO tool to *this* checkout's langgraph service.

    `uv run` installs every workspace member editable, so each service source
    directory is already on `sys.path` — and `services/api` sits ahead of
    `services/langgraph` there. Both services own a top level package named
    `src`, so a plain `import src.agents...` binds to the API service and fails
    with `No module named 'src.agents'`. A conditional insert cannot repair
    that: the path is present, just in the losing position. The checkout is
    therefore forced to the front of `sys.path`, and the module the import
    actually produced is checked against the checkout before it is used, so a
    wrong binding is a named preflight failure rather than a missing attribute
    somewhere later.
    """
    for entry in (root, root / "services" / "langgraph"):
        text = str(entry)
        while text in sys.path:
            sys.path.remove(text)
        sys.path.insert(0, text)
    from src.agents.po import tools_projects
    from src.agents.po.tools import create_project, init_po_clients

    expected = root / "services" / "langgraph" / "src" / "agents" / "po" / "tools_projects.py"
    if Path(tools_projects.__file__).resolve() != expected.resolve():
        raise PreflightError(
            f"{TOOL_IDENTIFIER} resolved outside the matrix checkout: {tools_projects.__file__}"
        )
    if create_project is not tools_projects.create_project:
        raise PreflightError(f"{TOOL_IDENTIFIER} is not the tool the preflight imported")
    return create_project, init_po_clients


class MatrixRuntime:
    """Production implementation backed by the matrix checkout and sidecar API."""

    def __init__(self, *, api_url: str, api_container: str) -> None:
        self._root = resolve_repo_root(Path(__file__))
        self._api_url = api_url
        self._api_container = api_container
        create_project, init_po_clients = load_po_tool_boundary(self._root)
        from shared.clients.internal_api import InternalAPIClient

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

    async def ensure_test_user(self, telegram_id: str) -> None:
        import pipeline_helpers

        expected_telegram_id = str(pipeline_helpers.TEST_TELEGRAM_ID)
        if telegram_id != expected_telegram_id:
            raise PreflightError("matrix preflight must use the live-harness test identity")
        async with pipeline_helpers.api_client_as_test_user(base_url=self._api_url) as api:
            await pipeline_helpers.ensure_test_user(api)

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

    @staticmethod
    def _redis_json_entries(
        result: subprocess.CompletedProcess[str], *, probe: str
    ) -> list[list[Any]]:
        if result.returncode != 0:
            raise PreflightError(f"{probe} probe failed")
        try:
            entries = json.loads(result.stdout or "[]")
        except json.JSONDecodeError as exc:
            raise PreflightError(f"{probe} probe was not machine-readable") from exc
        if not isinstance(entries, list):
            raise PreflightError(f"{probe} probe was not a list")
        return entries

    @staticmethod
    def _proactive_delta_entry(entry: list[Any]) -> dict[str, str | None]:
        if (
            not isinstance(entry, list)
            or len(entry) != 2
            or not isinstance(entry[0], str)
            or _STREAM_ID_RE.fullmatch(entry[0]) is None
            or not isinstance(entry[1], list)
        ):
            raise PreflightError("PO proactive stream probe entry was malformed")
        fields = entry[1]
        if len(fields) % 2:
            raise PreflightError("PO proactive stream probe fields were malformed")
        values = {str(fields[index]): str(fields[index + 1]) for index in range(0, len(fields), 2)}
        raw_project_id = values.get("project_id")
        if not raw_project_id:
            return {
                "stream_id": entry[0],
                "project_id": None,
                "project_identity": "missing",
            }
        try:
            project_id = str(uuid.UUID(raw_project_id))
        except ValueError:
            return {
                "stream_id": entry[0],
                "project_id": None,
                "project_identity": "invalid",
            }
        return {
            "stream_id": entry[0],
            "project_id": project_id,
            "project_identity": "valid",
        }

    def _proactive_boundary(self) -> dict[str, str | None]:
        latest = self._redis_json_entries(
            self._compose_redis("--json", "XREVRANGE", "po:proactive", "+", "-", "COUNT", "1"),
            probe="PO proactive boundary",
        )
        if not latest:
            return {"stream_id": None}
        if len(latest) != 1 or not isinstance(latest[0], list) or not latest[0]:
            raise PreflightError("PO proactive boundary entry was malformed")
        stream_id = latest[0][0]
        if not isinstance(stream_id, str) or _STREAM_ID_RE.fullmatch(stream_id) is None:
            raise PreflightError("PO proactive boundary identifier was malformed")
        return {"stream_id": stream_id}

    def _proactive_delta(self, proactive_after_id: str | None) -> list[dict[str, str | None]]:
        start = f"({proactive_after_id}" if proactive_after_id is not None else "-"
        entries = self._redis_json_entries(
            self._compose_redis("--json", "XRANGE", "po:proactive", start, "+"),
            probe="PO proactive delta",
        )
        return [self._proactive_delta_entry(entry) for entry in entries]

    async def notification_snapshot(
        self,
        *,
        request_ids: list[str],
        project_ids: list[str],
        telegram_id: str,
        phase: Literal["before", "after"],
        proactive_after_id: str | None,
    ) -> dict[str, Any]:
        if not project_ids or any(not isinstance(project_id, str) for project_id in project_ids):
            raise PreflightError("notification probe project scope is invalid")
        if not telegram_id:
            raise PreflightError("notification probe test identity is invalid")
        response_streams: dict[str, bool] = {}
        for request_id in request_ids:
            response = self._compose_redis("--raw", "EXISTS", f"po:response:{request_id}")
            if response.returncode != 0 or response.stdout.strip() not in {"0", "1"}:
                raise PreflightError("PO response stream probe failed")
            response_streams[request_id] = response.stdout.strip() == "1"

        if phase == "before":
            if proactive_after_id is not None:
                raise PreflightError("notification boundary cannot follow an earlier boundary")
            return {
                "po_response_streams": response_streams,
                "proactive_boundary": self._proactive_boundary(),
            }
        if phase == "after":
            return {
                "po_response_streams": response_streams,
                "proactive_delta": self._proactive_delta(proactive_after_id),
            }
        raise PreflightError("notification probe phase is invalid")

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
