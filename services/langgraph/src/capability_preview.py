"""The Architect's capability preview: deterministic Python, no model, no live branch.

The PO hands over what the user asked for in user-level terms (`CapabilityRequest`), and
this module answers, against the activated catalog snapshot, with a route per request and
the questions the user must answer before confirming. It is the only place that maps a
user-level capability to a catalog package, and it does so through the catalog's own
metadata: an offered capability is one released, ordinary catalog package with a default
binding, identified by an opaque id derived from it (`capability_id`); its questions come
from the binding's product settings and the package manifest's own settings schema; its
limitations from the manifest's platform quota. Nothing here guesses a mapping or invents
a key: metadata the preview cannot turn into a question is a typed refusal.

A module route is resolved to the exact closure `plan_install_payload` selects from the same
snapshot — package, version, tag, recommended libraries, default binding — before the user is
asked anything, so a capability the preview offers is one that installs on this core.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from hashlib import sha256
import uuid

from framework.binding_product import binding_settings
from framework.bindings_v2 import BindingV2
from framework.spec.loader import _package_prefix
from framework.spec.packages import PackageManifest
import structlog
import yaml

from shared.contracts.dto.capability_preview import (
    CAPABILITY_LOCALES,
    AnswerTarget,
    CapabilityOffer,
    CapabilityPreviewCreate,
    CapabilityPreviewProjection,
    CapabilityPreviewRefusal,
    CapabilityPreviewTechnical,
    CapabilityRequest,
    CapabilityRoute,
    PreviewLimitation,
    PreviewModule,
    PreviewQuestion,
    PreviewRefusalCode,
    PreviewRoute,
    QuestionKind,
    RouteReason,
)
from shared.contracts.dto.catalog_install import CatalogActivation

from .catalog_install import (
    INSTALL_PYTHON_VERSION,
    InstallRefusal,
    load_catalog_binding,
    plan_install_payload,
)
from .kit_catalog import (
    TERMINAL_CATALOG_FAILURES,
    InstallablePackage,
    KitCatalog,
    KitCatalogAnswer,
)

logger = structlog.get_logger(__name__)

#: The product shape every catalog module installs into.
MODULE_PRODUCT_SHAPE = frozenset({"backend", "tg_bot"})
#: Package environment sources only the platform can provide: no product code replaces them.
PLATFORM_SOURCES = frozenset({"platform_key", "platform_base_url"})
#: The question ids of the settings every binding shares, product-wide.
LANGUAGE_QUESTION = "product_language"
TIMEZONE_QUESTION = "product_timezone"
#: JSON Schema keywords a text-list or choice answer can be checked against exactly.
_LIST_KEYWORDS = frozenset({"type", "items", "maxItems", "uniqueItems", "description"})
_ITEM_KEYWORDS = frozenset({"type", "pattern", "maxLength", "description"})
_CHOICE_KEYWORDS = frozenset({"type", "enum", "description"})


def capability_id(package_name: str) -> str:
    """The opaque id of the capability one catalog package offers; stable across turns."""
    return "cap-" + sha256(f"kit-package:{package_name}".encode()).hexdigest()[:12]


def _offered(item: InstallablePackage) -> bool:
    package = item.package
    return package.extends is None and package.default_binding is not None


def capability_offers(catalog: KitCatalog) -> list[CapabilityOffer]:
    """What the activated catalog offers as ready modules, in user-level words only."""
    return [
        CapabilityOffer(
            capability_id=capability_id(item.name),
            summary=item.package.summary,
            phrases=list(item.package.capabilities),
        )
        for item in catalog.packages
        if _offered(item)
    ]


class PreviewRefused(Exception):  # noqa: N818 - a typed answer, raised to unwind one preview
    def __init__(self, code: PreviewRefusalCode, request_ids: list[str] | None = None) -> None:
        self.refusal = CapabilityPreviewRefusal(code=code, request_ids=sorted(request_ids or []))
        super().__init__(code.value)


@dataclass
class _Question:
    question: PreviewQuestion
    target: AnswerTarget


def _manifest(catalog: KitCatalog, item: InstallablePackage) -> PackageManifest:
    return PackageManifest.model_validate(yaml.safe_load(catalog.manifests[item.name]))


def _needs_platform(manifest: PackageManifest) -> bool:
    return any(
        entry.source is not None and entry.source.kind in PLATFORM_SOURCES
        for entry in manifest.environment
    )


def _binding_questions(catalog: KitCatalog, item: InstallablePackage, request_id: str):
    binding = load_catalog_binding(catalog.bindings[item.name])
    for key, schema in binding_settings(binding).items():
        if isinstance(binding, BindingV2) and key == binding.language.key:
            choices = [
                value
                for value in CAPABILITY_LOCALES
                if value in binding.language.values and value in schema.get("enum", [])
            ]
            if not choices:
                raise PreviewRefused(PreviewRefusalCode.UNSUPPORTED_QUESTION, [request_id])
            kind, question_id = QuestionKind.PRODUCT_LANGUAGE, LANGUAGE_QUESTION
        elif schema.get("format") == "x-iana-tz":
            choices, kind, question_id = [], QuestionKind.TIMEZONE, TIMEZONE_QUESTION
        else:
            raise PreviewRefused(PreviewRefusalCode.UNSUPPORTED_QUESTION, [request_id])
        required = "default" not in schema
        yield _Question(
            PreviewQuestion(
                question_id=question_id,
                request_ids=[request_id],
                kind=kind,
                required=required,
                choices=choices,
            ),
            AnswerTarget(
                question_id=question_id,
                key=key,
                kind=kind,
                required=required,
                choices=choices,
                schema=schema,
            ),
        )


def _package_questions(manifest: PackageManifest, name: str, request_id: str):
    """One question per manifest setting whose answer shape the preview can check exactly.

    A required setting of any other shape refuses the preview; an optional one is left to
    the package's seed or default and asked about not at all.
    """
    seeds = {seed.key for seed in manifest.setting_seeds}
    properties = manifest.settings_schema.get("properties", {})
    number = 0
    for local_key, schema in properties.items():
        required = local_key not in seeds and "default" not in schema
        shape = _answer_shape(schema)
        if shape is None:
            if required:
                raise PreviewRefused(PreviewRefusalCode.UNSUPPORTED_QUESTION, [request_id])
            continue
        number += 1
        question_id = f"{request_id}.q{number}"
        kind, constraints = shape
        yield _Question(
            PreviewQuestion(
                question_id=question_id,
                request_ids=[request_id],
                kind=kind,
                required=required,
                choices=constraints.get("choices", []),
                max_items=constraints.get("max_items"),
                item_pattern=constraints.get("item_pattern"),
            ),
            AnswerTarget(
                question_id=question_id,
                key=f"{_package_prefix(name)}.{local_key}",
                kind=kind,
                required=required,
                schema=schema,
                **constraints,
            ),
        )


def _answer_shape(schema: dict) -> tuple[QuestionKind, dict] | None:
    if schema.get("type") == "array" and set(schema) <= _LIST_KEYWORDS:
        items = schema.get("items")
        if not isinstance(items, dict) or items.get("type") != "string":
            return None
        if not set(items) <= _ITEM_KEYWORDS:
            return None
        return QuestionKind.TEXT_LIST, {
            "max_items": schema.get("maxItems"),
            "item_pattern": items.get("pattern"),
            "item_max_length": items.get("maxLength"),
            "unique_items": bool(schema.get("uniqueItems", False)),
        }
    if schema.get("type") == "string" and "enum" in schema and set(schema) <= _CHOICE_KEYWORDS:
        if all(isinstance(value, str) for value in schema["enum"]):
            return QuestionKind.CHOICE, {"choices": list(schema["enum"])}
    return None


def _limitations(manifest: PackageManifest, request_id: str) -> list[PreviewLimitation]:
    return [
        PreviewLimitation(request_id=request_id, name=name, value=value)
        for entry in manifest.environment
        if entry.source is not None and entry.source.kind == "platform_key"
        for name, value in entry.source.quota.items()
        if isinstance(value, int) and not isinstance(value, bool)
    ]


def _merge(questions: dict[str, _Question], incoming: _Question) -> None:
    """One question per answer target; two modules sharing a product setting share it."""
    current = questions.get(incoming.question.question_id)
    if current is None:
        questions[incoming.question.question_id] = incoming
        return
    a, b = current.target, incoming.target
    if (a.key, a.kind, a.required, a.choices) != (b.key, b.kind, b.required, b.choices):
        raise PreviewRefused(
            PreviewRefusalCode.CONFLICTING_QUESTION,
            [*current.question.request_ids, *incoming.question.request_ids],
        )
    current.question.request_ids.extend(
        key for key in incoming.question.request_ids if key not in current.question.request_ids
    )


def resolve_preview(  # noqa: C901, PLR0912 - one route decision per request, in order
    *,
    project_id: uuid.UUID,
    requests: list[CapabilityRequest],
    catalog: KitCatalogAnswer,
    activation: CatalogActivation,
    project_modules: set[str],
    rollout_admits: bool,
    platform_cannot: Callable[[str], object | None],
) -> CapabilityPreviewCreate:
    """The stored preview for these requests, or `PreviewRefused`.

    Deterministic: the same requests, project facts and snapshot give the same preview.
    """
    named = [request for request in requests if request.capability_id is not None]
    offered: dict[str, InstallablePackage] = {}
    if named:
        if not isinstance(catalog, KitCatalog):
            code = (
                PreviewRefusalCode.CATALOG_INACTIVE
                if catalog.failure in TERMINAL_CATALOG_FAILURES
                else PreviewRefusalCode.CATALOG_UNAVAILABLE
            )
            raise PreviewRefused(code, [request.request_id for request in named])
        offered = {capability_id(item.name): item for item in catalog.packages if _offered(item)}
        if unknown := [r.request_id for r in named if r.capability_id not in offered]:
            raise PreviewRefused(PreviewRefusalCode.UNKNOWN_CAPABILITY, unknown)
    routes: list[PreviewRoute] = []
    modules: list[PreviewModule] = []
    questions: dict[str, _Question] = {}
    limitations: list[PreviewLimitation] = []
    for request in requests:
        if request.capability_id is None:
            cannot = platform_cannot(f"{request.wording} {request.beyond or ''}")
            route, reason = (
                (CapabilityRoute.IMPOSSIBLE, RouteReason.PLATFORM_CANNOT)
                if cannot is not None
                else (CapabilityRoute.FROM_SCRATCH, RouteReason.NOT_OFFERED)
            )
            routes.append(PreviewRoute(request_id=request.request_id, route=route, reason=reason))
            continue
        assert isinstance(catalog, KitCatalog)
        item = offered[request.capability_id]
        try:
            manifest = _manifest(catalog, item)
            outside = route_outside_rollout(
                manifest,
                shape_ok=MODULE_PRODUCT_SHAPE <= project_modules,
                rollout_admits=rollout_admits,
            )
            if outside is not None:
                routes.append(
                    PreviewRoute(
                        request_id=request.request_id,
                        capability_id=request.capability_id,
                        route=outside[0],
                        reason=outside[1],
                    )
                )
                continue
            install = plan_install_payload(catalog, item.name, INSTALL_PYTHON_VERSION)
            found = [
                *_binding_questions(catalog, item, request.request_id),
                *_package_questions(manifest, item.name, request.request_id),
            ]
        except PreviewRefused:
            raise
        except (InstallRefusal, ValueError, KeyError) as error:
            logger.warning(
                "capability_preview_unresolvable",
                request_id=request.request_id,
                error=str(error),
            )
            raise PreviewRefused(
                PreviewRefusalCode.CAPABILITY_UNRESOLVABLE, [request.request_id]
            ) from error
        for question in found:
            _merge(questions, question)
        limitations.extend(_limitations(manifest, request.request_id))
        beyond = request.beyond is not None
        routes.append(
            PreviewRoute(
                request_id=request.request_id,
                capability_id=request.capability_id,
                route=CapabilityRoute.MODULE_WITH_GLUE if beyond else CapabilityRoute.MODULE,
                reason=RouteReason.BEYOND_OFFER if beyond else RouteReason.OFFERED,
            )
        )
        modules.append(
            PreviewModule(
                request_id=request.request_id,
                capability_id=request.capability_id,
                install=install,
            )
        )
    return CapabilityPreviewCreate(
        project_id=project_id,
        requests=requests,
        product=CapabilityPreviewProjection(
            routes=routes,
            questions=[item.question for item in questions.values()],
            limitations=limitations,
        ),
        technical=CapabilityPreviewTechnical(
            activation=activation,
            modules=modules,
            targets=[item.target for item in questions.values()],
        ),
    )


def route_outside_rollout(
    manifest: PackageManifest, *, shape_ok: bool, rollout_admits: bool
) -> tuple[CapabilityRoute, RouteReason] | None:
    """The honest route of an offered capability this project may not install, if any.

    Ordinary product code can provide what a module provides only when the module needs
    nothing the platform alone supplies; otherwise the capability is impossible here.
    """
    if shape_ok and rollout_admits:
        return None
    reason = RouteReason.ROLLOUT_NOT_ENABLED if shape_ok else RouteReason.PRODUCT_SHAPE
    if _needs_platform(manifest):
        return CapabilityRoute.IMPOSSIBLE, reason
    return CapabilityRoute.FROM_SCRATCH, reason
