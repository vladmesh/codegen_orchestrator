"""Capability preview: what a user-level capability means before a brief is confirmed.

Two projections of one Architect decision, kept apart by type:

* the *product* projection (`CapabilityPreviewProjection`, `CapabilityPreviewRead`) is the
  only one the PO, its tools and the user ever see: opaque capability ids and the user-level
  phrases behind them, the route, question ids with their kinds and choices, and
  limitations. No package, module, version, catalog entry, install recipe or settings key
  appears in it.
* the *technical* projection (`CapabilityPreviewTechnical`) is stored beside the preview and
  read only by Python: the activated catalog snapshot, the install closure of every module
  route, and the exact settings key, scope and constraints each answer is written to.

A brief that relies on a preview carries only the product half (`BriefCapabilities`): the
preview id, the requirements each capability serves, and the user's answers. When the API
opens that revision it derives the `CapabilityPlan` from the stored technical half and those
answers (`derive_capability_plan`) and stores it beside the revision; confirmation derives it
again and refuses any drift, so a confirmed revision and its plan are one decision.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
import re
from typing import Annotated, Any, Literal
import uuid
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from shared.contracts.dto.catalog_install import CatalogActivation, CatalogInstall

#: The product languages a capability question may offer; the user picks one explicitly.
CAPABILITY_LOCALES = ("ru", "en")
#: How many capabilities one preview, and so one brief, may rely on.
MAX_CAPABILITY_REQUESTS = 4
MAX_CAPABILITY_ANSWERS = 8
MAX_CAPABILITY_WORDING_LENGTH = 250
MAX_CAPABILITY_BEYOND_LENGTH = 200

RequestId = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")]
#: Opaque, stable across turns and activations, and naming no package.
CapabilityId = Annotated[str, StringConstraints(pattern=r"^cap-[0-9a-f]{12}$")]
QuestionId = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,95}$")]
PreviewId = Annotated[str, StringConstraints(pattern=r"^preview-[0-9a-f]{24}$")]
SettingKey = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)*$")]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CapabilityRoute(StrEnum):
    """How a requested capability would be provided. The route, not the result.

    A module route says which released capability resolution covers the request; whether
    the product needs glue around it beyond what the request names is decided when the
    module is installed, not here.
    """

    #: A released catalog module covers the request as offered.
    MODULE = "module"
    #: A released catalog module, plus product work for what the user wants beyond it.
    MODULE_WITH_GLUE = "module_with_glue"
    #: Ordinary product code; no catalog module is used.
    FROM_SCRATCH = "from_scratch"
    #: The platform cannot provide it for this project now.
    IMPOSSIBLE = "impossible"


MODULE_ROUTES = frozenset({CapabilityRoute.MODULE, CapabilityRoute.MODULE_WITH_GLUE})


class RouteReason(StrEnum):
    """Why the preview chose the route, in terms the PO can explain to the user."""

    OFFERED = "offered"
    BEYOND_OFFER = "beyond_offer"
    #: The request names no capability the platform offers as a module.
    NOT_OFFERED = "not_offered"
    #: The project is not enabled for module-backed capabilities yet.
    ROLLOUT_NOT_ENABLED = "rollout_not_enabled"
    #: The capability needs a bot with a backend, which this project is not.
    PRODUCT_SHAPE = "product_shape"
    #: The platform manifest says products cannot have this.
    PLATFORM_CANNOT = "platform_cannot"


class QuestionKind(StrEnum):
    """The shape of an answer; the technical target says where it is written."""

    #: One of `choices` (a subset of `CAPABILITY_LOCALES`). Never inferred from the chat.
    PRODUCT_LANGUAGE = "product_language"
    #: An IANA time zone name, e.g. `Europe/Moscow`.
    TIMEZONE = "timezone"
    #: A list of short texts, each matching `item_pattern`, at most `max_items`.
    TEXT_LIST = "text_list"
    #: One of `choices`.
    CHOICE = "choice"


class CapabilityOffer(_Strict):
    """A capability the activated catalog offers as a ready module, in user-level words."""

    capability_id: CapabilityId
    summary: str
    phrases: list[str]


class ModuleRollout(_Strict):
    """Which projects module-backed routes are enabled for (`capabilities.module_rollout`).

    The catalog is not a feature whitelist: a project outside this list still gets every
    ordinary programmable feature, and a request for a module-backed capability is routed
    from scratch or, where only a platform module can provide it, as impossible.
    """

    project_ids: list[uuid.UUID] = Field(default_factory=list)

    def admits(self, project_id: uuid.UUID) -> bool:
        return project_id in self.project_ids


class PreviewRefusalCode(StrEnum):
    """Why no preview was stored. Product-safe: no package, key or catalog detail."""

    #: A request named a capability id the activated catalog does not offer.
    UNKNOWN_CAPABILITY = "unknown_capability"
    #: A request without an id whose words name more than one offered capability.
    AMBIGUOUS_CAPABILITY = "ambiguous_capability"
    #: The activated catalog could not be read now; a retry may succeed.
    CATALOG_UNAVAILABLE = "catalog_unavailable"
    #: The activated catalog does not match this host or its digests; an operator must act.
    CATALOG_INACTIVE = "catalog_inactive"
    #: The module rollout policy could not be read now.
    ROLLOUT_UNAVAILABLE = "rollout_unavailable"
    #: The offered module does not resolve to an installable closure for this core.
    CAPABILITY_UNRESOLVABLE = "capability_unresolvable"
    #: The module needs an answer whose shape the preview cannot ask for.
    UNSUPPORTED_QUESTION = "unsupported_question"
    #: Two requested modules need the same setting with incompatible answers.
    CONFLICTING_QUESTION = "conflicting_question"


class CapabilityPreviewRefusal(_Strict):
    code: PreviewRefusalCode
    request_ids: list[str] = Field(default_factory=list)


class CapabilityRequest(_Strict):
    """One capability the user asked for, as the PO hands it to the preview."""

    request_id: RequestId
    #: An id the PO's capability list offered, or None for anything else the user wants.
    #: Optional only for the PO: words that contain an offered capability's catalog phrase
    #: resolve to that capability anyway, and its route names it.
    capability_id: CapabilityId | None = None
    #: The user's words for it.
    wording: str = Field(min_length=1, max_length=MAX_CAPABILITY_WORDING_LENGTH)
    #: What the user wants beyond the offered capability, if anything.
    beyond: str | None = Field(default=None, min_length=1, max_length=MAX_CAPABILITY_BEYOND_LENGTH)


class PreviewRoute(_Strict):
    request_id: RequestId
    capability_id: CapabilityId | None = None
    route: CapabilityRoute
    reason: RouteReason


class PreviewQuestion(_Strict):
    """A question the user answers before confirming; product-only."""

    question_id: QuestionId
    request_ids: list[RequestId] = Field(min_length=1)
    kind: QuestionKind
    required: bool
    choices: list[str] = Field(default_factory=list)
    max_items: int | None = Field(default=None, ge=1)
    #: The form of one list item (a regular expression), so the PO can ask for it exactly.
    item_pattern: str | None = None


class PreviewLimitation(_Strict):
    """A limit the user should know before confirming, e.g. how many channels at most."""

    request_id: RequestId
    kind: Literal["quota"] = "quota"
    name: Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{0,63}$")]
    value: int = Field(ge=0)


class CapabilityPreviewProjection(_Strict):
    """What the PO and the user may see of a preview."""

    routes: list[PreviewRoute] = Field(min_length=1, max_length=MAX_CAPABILITY_REQUESTS)
    questions: list[PreviewQuestion] = Field(default_factory=list)
    limitations: list[PreviewLimitation] = Field(default_factory=list)


class CapabilityPreviewRead(CapabilityPreviewProjection):
    """A stored preview, product projection only."""

    model_config = ConfigDict(extra="forbid", from_attributes=True)

    preview_id: PreviewId
    project_id: uuid.UUID
    created_at: datetime


class AnswerTarget(_Strict):
    """Where one question's answer is written in the product, and what it must satisfy."""

    question_id: QuestionId
    key: SettingKey
    scope: Literal["product"] = "product"
    kind: QuestionKind
    required: bool
    choices: list[str] = Field(default_factory=list)
    max_items: int | None = Field(default=None, ge=1)
    item_pattern: str | None = None
    item_max_length: int | None = Field(default=None, ge=1)
    unique_items: bool = False
    #: The declaring JSON Schema, kept as the record of what was verified.
    schema_data: dict = Field(alias="schema")

    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class PreviewModule(_Strict):
    """The released closure a module route installs, resolved from the activated snapshot."""

    request_id: RequestId
    capability_id: CapabilityId
    install: CatalogInstall

    @model_validator(mode="after")
    def _pinned(self) -> PreviewModule:
        if self.install.catalog is None:
            raise ValueError("a previewed module names the catalog commit it installs from")
        return self


class CapabilityPreviewTechnical(_Strict):
    activation: CatalogActivation
    modules: list[PreviewModule] = Field(default_factory=list)
    targets: list[AnswerTarget] = Field(default_factory=list)


class CapabilityPreviewCreate(_Strict):
    """Store one preview: internal callers only (the PO tool, never a user)."""

    project_id: uuid.UUID
    requests: list[CapabilityRequest] = Field(min_length=1, max_length=MAX_CAPABILITY_REQUESTS)
    product: CapabilityPreviewProjection
    technical: CapabilityPreviewTechnical

    @model_validator(mode="after")
    def _one_decision(self) -> CapabilityPreviewCreate:
        requests = {request.request_id: request for request in self.requests}
        if len(requests) != len(self.requests):
            raise ValueError("capability request ids must be unique")
        routes = {route.request_id: route for route in self.product.routes}
        if routes.keys() != requests.keys() or len(routes) != len(self.product.routes):
            raise ValueError("a preview routes every request exactly once")
        if any(
            requests[key].capability_id not in (None, routes[key].capability_id) for key in requests
        ):
            raise ValueError("a route names the capability its request named")
        modules = {module.request_id: module for module in self.technical.modules}
        expected = {key for key, route in routes.items() if route.route in MODULE_ROUTES}
        if modules.keys() != expected or len(modules) != len(self.technical.modules):
            raise ValueError("every module route, and only one, has a resolved closure")
        if any(modules[key].capability_id != routes[key].capability_id for key in modules):
            raise ValueError("a closure belongs to its route's capability")
        source = self.technical.activation.source()
        if any(module.install.catalog != source for module in self.technical.modules):
            raise ValueError("every closure is resolved from the activated snapshot")
        questions = {question.question_id: question for question in self.product.questions}
        targets = {target.question_id: target for target in self.technical.targets}
        if questions.keys() != targets.keys() or len(questions) != len(self.product.questions):
            raise ValueError("every question has exactly one answer target")
        for key, question in questions.items():
            target = targets[key]
            if (question.kind, question.required, question.choices, question.max_items) != (
                target.kind,
                target.required,
                target.choices,
                target.max_items,
            ):
                raise ValueError(f"question {key} and its target disagree")
            if not set(question.request_ids) <= expected:
                raise ValueError(f"question {key} belongs to a module route")
        return self


class CapabilityAnswer(_Strict):
    """The user's explicit answer to one preview question."""

    question_id: QuestionId
    kind: QuestionKind
    value: str | list[str]
    #: What the answer means, in the user's language: the only way the user is shown it.
    description: str = Field(min_length=1, max_length=150)


class BriefCapability(_Strict):
    """One capability a brief relies on, and the must-requirements it serves."""

    request_id: RequestId
    capability_id: CapabilityId | None = None
    route: CapabilityRoute
    requirement_ids: list[str] = Field(min_length=1, max_length=8)


class BriefCapabilities(_Strict):
    """The product half of a capability-backed brief: preview, capabilities and answers."""

    preview_id: PreviewId
    capabilities: list[BriefCapability] = Field(min_length=1, max_length=MAX_CAPABILITY_REQUESTS)
    answers: list[CapabilityAnswer] = Field(default_factory=list, max_length=MAX_CAPABILITY_ANSWERS)

    @model_validator(mode="after")
    def _unique(self) -> BriefCapabilities:
        requests = [capability.request_id for capability in self.capabilities]
        questions = [answer.question_id for answer in self.answers]
        if len(requests) != len(set(requests)) or len(questions) != len(set(questions)):
            raise ValueError("capabilities and answers are each named once")
        return self


class PlannedSetting(_Strict):
    question_id: QuestionId
    key: SettingKey
    scope: Literal["product"] = "product"
    value: Any


class PlannedCapability(_Strict):
    request_id: RequestId
    capability_id: CapabilityId | None = None
    route: CapabilityRoute
    requirement_ids: list[str] = Field(min_length=1)
    #: The closure a module route installs, exactly as resolved at preview; None otherwise.
    install: CatalogInstall | None = None

    @model_validator(mode="after")
    def _closure_iff_module(self) -> PlannedCapability:
        if (self.install is not None) != (self.route in MODULE_ROUTES):
            raise ValueError("a planned capability has a closure exactly when it is a module")
        if self.install is not None and self.install.catalog is None:
            raise ValueError("a planned closure names the catalog commit it installs from")
        return self


class CapabilityPlan(_Strict):
    """The technical plan frozen beside one brief revision; never shown to the PO or user."""

    preview_id: PreviewId
    activation: CatalogActivation
    capabilities: list[PlannedCapability] = Field(min_length=1)
    settings: list[PlannedSetting] = Field(default_factory=list)

    @model_validator(mode="after")
    def _one_value_per_target(self) -> CapabilityPlan:
        targets = [(setting.key, setting.scope) for setting in self.settings]
        if len(targets) != len(set(targets)):
            raise ValueError("a capability plan holds one value per setting key and scope")
        return self

    @property
    def modules(self) -> list[PlannedCapability]:
        return [item for item in self.capabilities if item.install is not None]

    @property
    def glue_requirement_ids(self) -> set[str]:
        """Requirements a module serves only with product work beyond it: no install covers them."""
        return {
            requirement_id
            for item in self.capabilities
            if item.route is CapabilityRoute.MODULE_WITH_GLUE
            for requirement_id in item.requirement_ids
        }


class PlanTask(_Strict):
    """What glue coverage reads of one task planned under the attempt being admitted."""

    task_id: str
    install: CatalogInstall | None = None
    blocked_by_task_id: str | None = None


def glue_coverage_gaps(
    plan: CapabilityPlan, covering: dict[str, str | None], tasks: dict[str, PlanTask]
) -> list[str]:
    """Glue requirements whose recorded covering task is not product work after their install.

    A `module_with_glue` requirement is covered by an ordinary task that runs after the
    INSTALL of that module's stored closure (its `blocked_by` chain reaches it), or it is
    explicitly returned (`covering[id] is None`). An INSTALL, or a task planned outside that
    chain, is not glue work, so the requirement is still outstanding. Requirements with no
    disposition at all are left to the completeness check.
    """
    gaps: set[str] = set()
    for item in plan.capabilities:
        if item.route is not CapabilityRoute.MODULE_WITH_GLUE:
            continue
        for requirement_id in item.requirement_ids:
            if requirement_id not in covering or covering[requirement_id] is None:
                continue
            task = tasks.get(covering[requirement_id])
            if (
                task is None
                or task.install is not None
                or not _runs_after(task, item.install, tasks)
            ):
                gaps.add(requirement_id)
    return sorted(gaps)


def _runs_after(task: PlanTask, install: CatalogInstall | None, tasks: dict[str, PlanTask]) -> bool:
    seen = {task.task_id}
    current = tasks.get(task.blocked_by_task_id or "")
    while current is not None and current.task_id not in seen:
        if current.install is not None and current.install == install:
            return True
        seen.add(current.task_id)
        current = tasks.get(current.blocked_by_task_id or "")
    return False


class CapabilityRefusalCode(StrEnum):
    """Why a capability-backed revision was not opened or confirmed. Product-safe codes."""

    PREVIEW_UNKNOWN = "preview_unknown"
    PREVIEW_FOREIGN = "preview_foreign"
    PREVIEW_STALE = "preview_stale"
    CAPABILITIES_MISMATCH = "capabilities_mismatch"
    IMPOSSIBLE_CAPABILITY = "impossible_capability"
    UNKNOWN_REQUIREMENT = "unknown_requirement"
    UNKNOWN_QUESTION = "unknown_question"
    MISSING_ANSWER = "missing_answer"
    INVALID_ANSWER = "invalid_answer"
    SETTING_CONFLICT = "setting_conflict"
    PLAN_MISSING = "plan_missing"
    PLAN_DRIFT = "plan_drift"


class CapabilityRefusal(_Strict):
    """A typed refusal naming only request and question ids, never a key or package."""

    code: CapabilityRefusalCode
    request_ids: list[str] = Field(default_factory=list)
    question_ids: list[str] = Field(default_factory=list)


class CapabilityPlanRefusedError(ValueError):
    def __init__(self, refusal: CapabilityRefusal) -> None:
        self.refusal = refusal
        super().__init__(refusal.code.value)


def _refuse(
    code: CapabilityRefusalCode,
    *,
    request_ids: list[str] | None = None,
    question_ids: list[str] | None = None,
) -> CapabilityPlanRefusedError:
    return CapabilityPlanRefusedError(
        CapabilityRefusal(
            code=code,
            request_ids=sorted(request_ids or []),
            question_ids=sorted(question_ids or []),
        )
    )


def answer_is_valid(target: AnswerTarget, value: str | list[str]) -> bool:
    """Whether `value` is an answer `target` accepts. Pure; the API and the PO share it."""
    match target.kind:
        case QuestionKind.PRODUCT_LANGUAGE | QuestionKind.CHOICE:
            return isinstance(value, str) and value in target.choices
        case QuestionKind.TIMEZONE:
            if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z_+\-/0-9]{1,64}", value):
                return False
            try:
                ZoneInfo(value)
            except (ZoneInfoNotFoundError, ValueError):
                return False
            return True
        case QuestionKind.TEXT_LIST:
            if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
                return False
            if target.max_items is not None and len(value) > target.max_items:
                return False
            if target.unique_items and len(value) != len(set(value)):
                return False
            return all(
                (target.item_pattern is None or re.fullmatch(target.item_pattern, item))
                and (target.item_max_length is None or len(item) <= target.item_max_length)
                for item in value
            )
    return False


def _owned_by(target_key: str, setting_key: str) -> bool:
    """A setting the target writes, or any key in the namespace of a package-owned target."""
    if setting_key == target_key:
        return True
    namespace, _, _ = target_key.rpartition(".")
    return bool(namespace) and setting_key.startswith(namespace + ".")


def derive_capability_plan(  # noqa: C901, PLR0912 - one ordered validation of one decision
    *,
    preview_id: str,
    product: CapabilityPreviewProjection,
    technical: CapabilityPreviewTechnical,
    capabilities: BriefCapabilities,
    must_requirement_ids: set[str],
    initial_setting_keys: set[str],
    activation: CatalogActivation,
) -> CapabilityPlan:
    """The plan a brief's capabilities and answers resolve to under this stored preview.

    Deterministic: the same preview, brief capabilities and activation always give the
    same plan, and nothing here reads a catalog. Refuses with a typed, product-safe
    `CapabilityPlanRefusedError` instead of planning around a stale preview, a capability
    the preview did not route, an impossible one, a missing or invalid answer, or an
    initial setting that would write a key an answer owns.
    """
    if capabilities.preview_id != preview_id:
        raise _refuse(CapabilityRefusalCode.PREVIEW_UNKNOWN)
    if technical.activation != activation:
        raise _refuse(CapabilityRefusalCode.PREVIEW_STALE)
    routes = {route.request_id: route for route in product.routes}
    impossible = sorted(
        item.request_id
        for item in capabilities.capabilities
        if item.route is CapabilityRoute.IMPOSSIBLE
        or (
            item.request_id in routes
            and routes[item.request_id].route is CapabilityRoute.IMPOSSIBLE
        )
    )
    if impossible:
        raise _refuse(CapabilityRefusalCode.IMPOSSIBLE_CAPABILITY, request_ids=impossible)
    expected = {
        key for key, route in routes.items() if route.route is not CapabilityRoute.IMPOSSIBLE
    }
    named = {item.request_id: item for item in capabilities.capabilities}
    mismatched = sorted(
        (expected ^ named.keys())
        | {
            key
            for key in expected & named.keys()
            if (named[key].capability_id, named[key].route)
            != (routes[key].capability_id, routes[key].route)
        }
    )
    if mismatched:
        raise _refuse(CapabilityRefusalCode.CAPABILITIES_MISMATCH, request_ids=mismatched)
    unknown_requirements = sorted(
        item.request_id
        for item in capabilities.capabilities
        if not set(item.requirement_ids) <= must_requirement_ids
    )
    if unknown_requirements:
        raise _refuse(CapabilityRefusalCode.UNKNOWN_REQUIREMENT, request_ids=unknown_requirements)
    targets = {target.question_id: target for target in technical.targets}
    answers = {answer.question_id: answer.value for answer in capabilities.answers}
    if unknown := sorted(answers.keys() - targets.keys()):
        raise _refuse(CapabilityRefusalCode.UNKNOWN_QUESTION, question_ids=unknown)
    if missing := sorted(
        key for key, target in targets.items() if target.required and key not in answers
    ):
        raise _refuse(CapabilityRefusalCode.MISSING_ANSWER, question_ids=missing)
    kinds = {answer.question_id: answer.kind for answer in capabilities.answers}
    if invalid := sorted(
        key
        for key, value in answers.items()
        if kinds[key] is not targets[key].kind or not answer_is_valid(targets[key], value)
    ):
        raise _refuse(CapabilityRefusalCode.INVALID_ANSWER, question_ids=invalid)
    conflicts = sorted(
        key
        for key, target in targets.items()
        if any(_owned_by(target.key, setting) for setting in initial_setting_keys)
    )
    if conflicts:
        raise _refuse(CapabilityRefusalCode.SETTING_CONFLICT, question_ids=conflicts)
    settings = _coherent_settings(technical.targets, answers)
    modules = {module.request_id: module for module in technical.modules}
    return CapabilityPlan(
        preview_id=preview_id,
        activation=technical.activation,
        capabilities=[
            PlannedCapability(
                request_id=item.request_id,
                capability_id=item.capability_id,
                route=item.route,
                requirement_ids=item.requirement_ids,
                install=modules[item.request_id].install if item.route in MODULE_ROUTES else None,
            )
            for item in capabilities.capabilities
        ],
        settings=settings,
    )


def _coherent_settings(
    targets: list[AnswerTarget], answers: dict[str, str | list[str]]
) -> list[PlannedSetting]:
    """One planned value per concrete setting target, or a typed refusal.

    Two answered questions can write the same key and scope — two requests selecting
    the same capability each ask its settings. Identical answers are one setting, kept
    under the first question in preview order; disagreeing answers refuse the plan
    with `SETTING_CONFLICT` naming those questions, so nothing downstream picks a
    winner by write order.
    """
    by_target: dict[tuple[str, str], list[AnswerTarget]] = {}
    for target in targets:
        if target.question_id in answers:
            by_target.setdefault((target.key, target.scope), []).append(target)
    disagreeing = sorted(
        target.question_id
        for group in by_target.values()
        if any(answers[item.question_id] != answers[group[0].question_id] for item in group)
        for target in group
    )
    if disagreeing:
        raise _refuse(CapabilityRefusalCode.SETTING_CONFLICT, question_ids=disagreeing)
    return [
        PlannedSetting(
            question_id=first.question_id,
            key=first.key,
            scope=first.scope,
            value=answers[first.question_id],
        )
        for first, *_ in by_target.values()
    ]
