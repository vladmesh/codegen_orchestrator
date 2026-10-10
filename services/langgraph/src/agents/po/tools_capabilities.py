"""PO tool — the Architect's capability preview, before a brief is presented.

`preview_capabilities` is the PO's only way to learn what a requested capability means
for a project. The decision is the Architect's deterministic Python
(`src.capability_preview`), made against the activated catalog snapshot, the project's
shape and the module rollout policy; the API stores it with its technical half. The PO
gets back the product projection only — preview id, routes, questions, limitations —
and every refusal is a typed code with request ids, never a package, key or catalog
detail.
"""

from __future__ import annotations

from http import HTTPStatus
import json
import uuid

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool
from pydantic import TypeAdapter, ValidationError
import structlog

from shared.catalog_activation import CATALOG_ACTIVATION
from shared.contracts.dto.capability_preview import (
    MAX_CAPABILITY_REQUESTS,
    CapabilityPreviewRead,
    CapabilityPreviewRefusal,
    CapabilityRequest,
    ModuleRollout,
    PreviewRefusalCode,
)

from ...capability_feasibility import platform_cannot
from ...capability_preview import PreviewRefused, resolve_preview
from ...catalog_product_settings import turn_catalog
from .tools_shared import _get_api, _user_headers

logger = structlog.get_logger(__name__)

#: The operator-owned system config naming the projects module routes are enabled for.
MODULE_ROLLOUT_CONFIG_KEY = "capabilities.module_rollout"

_REQUESTS = TypeAdapter(list[CapabilityRequest])

#: What the PO does next after each refusal; never the technical cause.
_NEXT_STEP = {
    PreviewRefusalCode.UNKNOWN_CAPABILITY: (
        "Use only capability ids from this turn's list, or null for anything else."
    ),
    PreviewRefusalCode.AMBIGUOUS_CAPABILITY: (
        "These words name more than one ready capability. Make one request per capability, "
        "each with its id from this turn's list."
    ),
    PreviewRefusalCode.CATALOG_UNAVAILABLE: (
        "The ready capabilities cannot be checked right now, so no request can be routed. "
        "Tell the user and try again in a later turn; do not present a brief that relies "
        "on these requests yet."
    ),
    PreviewRefusalCode.CATALOG_INACTIVE: (
        "Ready capabilities are switched off for maintenance. Tell the user they cannot be "
        "added now; offer to brief the rest of the product."
    ),
    PreviewRefusalCode.ROLLOUT_UNAVAILABLE: (
        "The platform could not say whether ready modules are enabled. Try again later."
    ),
    PreviewRefusalCode.CAPABILITY_UNRESOLVABLE: (
        "This ready capability cannot be added to this product now. Tell the user it is not "
        "possible now; do not promise it."
    ),
    PreviewRefusalCode.UNSUPPORTED_QUESTION: (
        "This ready capability needs a choice the platform cannot ask yet. Tell the user it "
        "is not possible now."
    ),
    PreviewRefusalCode.CONFLICTING_QUESTION: (
        "These capabilities need conflicting choices together. Preview them separately and "
        "ask the user which one to build first."
    ),
}


def _refused(refusal: CapabilityPreviewRefusal) -> str:
    return json.dumps(
        {
            "status": "preview_refused",
            **refusal.model_dump(mode="json"),
            "instruction": "No preview was stored. " + _NEXT_STEP[refusal.code],
        },
        ensure_ascii=False,
    )


async def _rollout() -> ModuleRollout | None:
    """The rollout policy, read as the service itself: it is platform configuration."""
    response = await _get_api().get_raw(f"system-configs/{MODULE_ROLLOUT_CONFIG_KEY}")
    if response.status_code != HTTPStatus.OK:
        logger.warning("po_module_rollout_unreadable", status=response.status_code)
        return None
    try:
        return ModuleRollout.model_validate(response.json()["value"])
    except (ValidationError, KeyError, TypeError) as error:
        logger.warning("po_module_rollout_invalid", error=str(error))
        return None


@tool
async def preview_capabilities(
    project_id: str, requests: list[dict], *, config: RunnableConfig
) -> str:
    """Ask the Architect what the requested capabilities mean for this project.

    Call it after `create_project` and before `present_product_brief` whenever the user
    wants a capability from this turn's ready-capability list, or an integration with an
    outside service. One request per capability:
    `{"request_id": "channels", "capability_id": "cap-...", "wording": "<the user's words>",
      "beyond": "<what they want past the listed capability, or omit>"}`.
    Use `capability_id: null` for anything not on the list. Words that describe a listed
    capability are routed as that capability even without its id, and the route names it.

    Returns the preview: `preview_id`, a `route` per request (`module`, `module_with_glue`,
    `from_scratch`, `impossible`) with its `reason`, the `questions` to ask the user
    (`question_id`, `kind`, `required`, `choices`, `max_items`, `item_pattern`) and
    `limitations`. Ask every required question explicitly; never infer the product
    language from the conversation. Pass the preview id, the routed capabilities and the
    answers to `present_product_brief(capabilities=...)`.

    Args:
        project_id: Project ID (UUID) from `create_project`.
        requests: The capability requests, at most 4.
    """
    try:
        project_uuid = uuid.UUID(project_id)
        parsed = _REQUESTS.validate_python(requests)
    except (ValueError, ValidationError) as invalid:
        return (
            "No preview was stored: the requests are not valid. Each request needs a "
            "path-safe `request_id`, the user's `wording` (at most 250 characters), "
            "`capability_id` from this turn's list or null, and optional `beyond` "
            f"(at most 200 characters); at most 4 requests.\n{invalid}"
        )
    if not 1 <= len(parsed) <= MAX_CAPABILITY_REQUESTS:
        return (
            "No preview was stored: give between 1 and "
            f"{MAX_CAPABILITY_REQUESTS} capability requests."
        )
    headers = _user_headers(config)
    api = _get_api()
    project = await api.get_raw(f"projects/{project_id}", headers=headers)
    if project.status_code != HTTPStatus.OK:
        return f"No preview was stored: project {project_id} cannot be read."
    modules = (project.json().get("config") or {}).get("modules")
    rollout = await _rollout()
    try:
        body = resolve_preview(
            project_id=project_uuid,
            requests=parsed,
            catalog=await turn_catalog(config),
            activation=CATALOG_ACTIVATION,
            project_modules=set(modules) if isinstance(modules, list) else set(),
            rollout_admits=None if rollout is None else rollout.admits(project_uuid),
            platform_cannot=platform_cannot,
        )
    except PreviewRefused as refused:
        logger.info(
            "po_capability_preview_refused",
            project_id=project_id,
            code=refused.refusal.code.value,
            request_ids=refused.refusal.request_ids,
        )
        return _refused(refused.refusal)
    # Stored by the platform acting for itself: no user identity authors a technical plan.
    response = await api.post_raw(
        "capability-previews/", json=body.model_dump(mode="json", by_alias=True)
    )
    if response.status_code != HTTPStatus.CREATED:
        logger.warning("po_capability_preview_not_stored", status=response.status_code)
        return "No preview was stored: the platform refused it. Try again in a later turn."
    preview = CapabilityPreviewRead.model_validate(response.json())
    logger.info(
        "po_capability_preview_stored",
        project_id=project_id,
        preview_id=preview.preview_id,
        routes=[route.route.value for route in preview.routes],
    )
    return json.dumps(
        {
            "status": "previewed",
            **preview.model_dump(mode="json", exclude={"project_id", "created_at"}),
            "instruction": (
                "Explain each route to the user in plain words. Ask every required question "
                "and wait for explicit answers; then present the brief with `capabilities`."
            ),
        },
        ensure_ascii=False,
    )


__all__ = ["MODULE_ROLLOUT_CONFIG_KEY", "preview_capabilities"]
