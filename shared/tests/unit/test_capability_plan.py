"""A brief's capabilities and answers resolve to one plan under one stored preview.

`derive_capability_plan` is the API's derivation at creation and again at confirmation,
so everything a forged, stale or mistaken brief could carry is refused here, as a typed
product-safe code, before any revision or plan exists.
"""

from __future__ import annotations

import uuid

from pydantic import ValidationError
import pytest

from shared.catalog_activation import CATALOG_ACTIVATION
from shared.contracts.dto.capability_preview import (
    AnswerTarget,
    BriefCapabilities,
    CapabilityPlanRefusedError,
    CapabilityPreviewCreate,
    CapabilityPreviewProjection,
    CapabilityPreviewTechnical,
    CapabilityRefusalCode,
    QuestionKind,
    answer_is_valid,
    derive_capability_plan,
)
from shared.contracts.dto.product_brief import ProposedProductBriefContent

PREVIEW_ID = "preview-" + "1" * 24
CHANNELS = "cap-5de1cd8d9a7c"


def _install() -> dict:
    return {
        "package": {
            "name": "tg-channels",
            "distribution": "codegen-kit-tg-channels",
            "version": "0.1.2",
            "tag": "packages/tg-channels/v0.1.2",
        },
        "libraries": [],
        "binding": {
            "package": "tg-channels",
            "resource": "codegen_kit_tg_channels:bindings/default.yaml",
            "sha256": "a" * 64,
            "functions": [],
        },
        "core_version": CATALOG_ACTIVATION.core_version,
        "python_version": "3.12.0",
        "catalog_digest": CATALOG_ACTIVATION.catalog_digest,
        "tooling_commit": CATALOG_ACTIVATION.tooling_commit,
        "catalog": CATALOG_ACTIVATION.source().model_dump(),
    }


LANGUAGE = {
    "question_id": "product_language",
    "kind": "product_language",
    "required": True,
    "choices": ["ru", "en"],
}
CHANNEL_LIST = {
    "question_id": "channels.q1",
    "kind": "text_list",
    "required": False,
    "max_items": 50,
}


def _preview(**technical_changes) -> CapabilityPreviewCreate:
    return CapabilityPreviewCreate.model_validate(
        {
            "project_id": str(uuid.uuid4()),
            "requests": [
                {"request_id": "channels", "capability_id": CHANNELS, "wording": "read channels"},
                {"request_id": "pay", "wording": "accept card payments"},
                {"request_id": "notes", "wording": "keep notes"},
            ],
            "product": {
                "routes": [
                    {
                        "request_id": "channels",
                        "capability_id": CHANNELS,
                        "route": "module",
                        "reason": "offered",
                    },
                    {"request_id": "pay", "route": "impossible", "reason": "platform_cannot"},
                    {"request_id": "notes", "route": "from_scratch", "reason": "not_offered"},
                ],
                "questions": [
                    {**LANGUAGE, "request_ids": ["channels"]},
                    {
                        **CHANNEL_LIST,
                        "request_ids": ["channels"],
                        "item_pattern": "^[A-Za-z][A-Za-z0-9_]{3,31}$",
                    },
                ],
                "limitations": [{"request_id": "channels", "name": "channels_max", "value": 50}],
            },
            "technical": {
                "activation": CATALOG_ACTIVATION.model_dump(),
                "modules": [
                    {"request_id": "channels", "capability_id": CHANNELS, "install": _install()}
                ],
                "targets": [
                    {**LANGUAGE, "key": "language", "schema": {"type": "string"}},
                    {
                        **CHANNEL_LIST,
                        "key": "tg_channels.starting_channels",
                        "item_pattern": "^[A-Za-z][A-Za-z0-9_]{3,31}$",
                        "unique_items": True,
                        "schema": {"type": "array"},
                    },
                ],
            }
            | technical_changes,
        }
    )


def _capabilities(**changes) -> BriefCapabilities:
    return BriefCapabilities.model_validate(
        {
            "preview_id": PREVIEW_ID,
            "capabilities": [
                {
                    "request_id": "channels",
                    "capability_id": CHANNELS,
                    "route": "module",
                    "requirement_ids": ["r1"],
                },
                {"request_id": "notes", "route": "from_scratch", "requirement_ids": ["r2"]},
            ],
            "answers": [
                {
                    "question_id": "product_language",
                    "kind": "product_language",
                    "value": "en",
                    "description": "The bot speaks English",
                },
                {
                    "question_id": "channels.q1",
                    "kind": "text_list",
                    "value": ["durov"],
                    "description": "Every user starts with @durov",
                },
            ],
        }
        | changes
    )


def _derive(capabilities=None, preview=None, *, initial=(), activation=CATALOG_ACTIVATION):
    preview = preview or _preview()
    return derive_capability_plan(
        preview_id=PREVIEW_ID,
        product=preview.product,
        technical=preview.technical,
        capabilities=capabilities or _capabilities(),
        must_requirement_ids={"r1", "r2"},
        initial_setting_keys=set(initial),
        activation=activation,
    )


def _refusal(**kwargs) -> CapabilityPlanRefusedError:
    with pytest.raises(CapabilityPlanRefusedError) as refused:
        _derive(**kwargs)
    return refused.value


def test_answers_resolve_to_exact_settings_and_the_module_to_its_stored_closure():
    plan = _derive()

    assert plan.preview_id == PREVIEW_ID and plan.activation == CATALOG_ACTIVATION
    [module, scratch] = plan.capabilities
    assert module.install is not None and module.install.model_dump(mode="json") == _install()
    assert module.requirement_ids == ["r1"]
    assert (scratch.route.value, scratch.install) == ("from_scratch", None)
    assert [(item.key, item.scope, item.value) for item in plan.settings] == [
        ("language", "product", "en"),
        ("tg_channels.starting_channels", "product", ["durov"]),
    ]


def test_identical_inputs_give_an_identical_plan():
    assert _derive().model_dump(mode="json") == _derive().model_dump(mode="json")


def test_an_unanswered_optional_question_writes_nothing():
    answers = [_capabilities().answers[0].model_dump()]
    plan = _derive(_capabilities(answers=answers))
    assert [item.key for item in plan.settings] == ["language"]


def test_a_preview_made_under_another_activation_is_stale():
    other = CATALOG_ACTIVATION.model_copy(update={"commit": "0" * 40})
    assert _refusal(activation=other).refusal.code is CapabilityRefusalCode.PREVIEW_STALE


def test_a_brief_naming_another_preview_is_refused():
    forged = _capabilities(preview_id="preview-" + "2" * 24)
    assert _refusal(capabilities=forged).refusal.code is CapabilityRefusalCode.PREVIEW_UNKNOWN


def test_an_impossible_capability_never_enters_a_plan():
    capabilities = _capabilities().model_dump()
    capabilities["capabilities"].append(
        {"request_id": "pay", "route": "from_scratch", "requirement_ids": ["r2"]}
    )
    refused = _refusal(capabilities=BriefCapabilities.model_validate(capabilities)).refusal
    assert (refused.code, refused.request_ids) == (
        CapabilityRefusalCode.IMPOSSIBLE_CAPABILITY,
        ["pay"],
    )


@pytest.mark.parametrize(
    "change",
    [
        {"route": "from_scratch"},
        {"capability_id": None},
    ],
    ids=["another-route", "another-capability"],
)
def test_a_capability_the_preview_routed_otherwise_is_a_mismatch(change):
    capabilities = _capabilities().model_dump()
    capabilities["capabilities"][0].update(change)
    refused = _refusal(capabilities=BriefCapabilities.model_validate(capabilities)).refusal
    assert (refused.code, refused.request_ids) == (
        CapabilityRefusalCode.CAPABILITIES_MISMATCH,
        ["channels"],
    )


def test_leaving_out_a_routed_capability_is_a_mismatch():
    capabilities = _capabilities().model_dump()
    del capabilities["capabilities"][1]
    refused = _refusal(capabilities=BriefCapabilities.model_validate(capabilities)).refusal
    assert (refused.code, refused.request_ids) == (
        CapabilityRefusalCode.CAPABILITIES_MISMATCH,
        ["notes"],
    )


def test_a_requirement_the_brief_does_not_have_is_refused():
    capabilities = _capabilities().model_dump()
    capabilities["capabilities"][0]["requirement_ids"] = ["r9"]
    refused = _refusal(capabilities=BriefCapabilities.model_validate(capabilities)).refusal
    assert refused.code is CapabilityRefusalCode.UNKNOWN_REQUIREMENT


def test_an_answer_to_a_question_never_asked_is_refused():
    answers = [*_capabilities().model_dump()["answers"]]
    answers.append(
        {
            "question_id": "channels.q9",
            "kind": "choice",
            "value": "x",
            "description": "Invented",
        }
    )
    refused = _refusal(capabilities=_capabilities(answers=answers)).refusal
    assert (refused.code, refused.question_ids) == (
        CapabilityRefusalCode.UNKNOWN_QUESTION,
        ["channels.q9"],
    )


def test_a_required_question_without_an_explicit_answer_is_refused():
    """The product language is never filled in from the conversation."""
    answers = [_capabilities().answers[1].model_dump()]
    refused = _refusal(capabilities=_capabilities(answers=answers)).refusal
    assert (refused.code, refused.question_ids) == (
        CapabilityRefusalCode.MISSING_ANSWER,
        ["product_language"],
    )


@pytest.mark.parametrize(
    "index,value,kind",
    [
        (0, "de", "product_language"),
        (0, "en", "choice"),
        (1, ["@durov"], "text_list"),
        (1, ["durov", "durov"], "text_list"),
        (1, "durov", "text_list"),
    ],
    ids=["locale", "kind", "pattern", "duplicate", "shape"],
)
def test_an_answer_outside_its_question_is_refused(index, value, kind):
    answers = _capabilities().model_dump()["answers"]
    answers[index].update(value=value, kind=kind)
    refused = _refusal(capabilities=_capabilities(answers=answers)).refusal
    assert refused.code is CapabilityRefusalCode.INVALID_ANSWER
    assert refused.question_ids == [answers[index]["question_id"]]


@pytest.mark.parametrize("key", ["language", "tg_channels.starting_channels", "tg_channels.x"])
def test_an_initial_setting_writing_what_an_answer_owns_is_refused(key):
    refused = _refusal(initial=[key]).refusal
    assert refused.code is CapabilityRefusalCode.SETTING_CONFLICT


def test_an_unrelated_initial_setting_stays_beside_the_plan():
    assert _derive(initial=["alerts.currency"]).settings


def test_a_refusal_names_no_key_package_or_version():
    refused = _refusal(initial=["tg_channels.starting_channels"]).refusal.model_dump_json()
    for technical in ("tg_channels", "tg-channels", "0.1.2", "packages/", "language"):
        assert technical not in refused


@pytest.mark.parametrize(
    "value,valid",
    [("Europe/Nicosia", True), ("UTC", True), ("Mars/Olympus", False), ("../etc", False)],
)
def test_a_timezone_answer_is_an_iana_name(value, valid):
    target = AnswerTarget(
        question_id="product_timezone",
        key="timezone",
        kind=QuestionKind.TIMEZONE,
        required=True,
        schema={"type": "string", "format": "x-iana-tz"},
    )
    assert answer_is_valid(target, value) is valid


def test_a_preview_whose_question_and_target_disagree_is_not_stored():
    preview = _preview().model_dump(mode="json", by_alias=True)
    preview["technical"]["targets"][0]["choices"] = ["en"]
    with pytest.raises(ValidationError, match="disagree"):
        CapabilityPreviewCreate.model_validate(preview)


def test_a_module_route_without_a_pinned_closure_is_not_stored():
    preview = _preview().model_dump(mode="json", by_alias=True)
    preview["technical"]["modules"][0]["install"]["catalog"] = None
    with pytest.raises(ValidationError, match="catalog commit"):
        CapabilityPreviewCreate.model_validate(preview)


def test_a_closure_from_another_snapshot_is_not_stored():
    preview = _preview().model_dump(mode="json", by_alias=True)
    preview["technical"]["modules"][0]["install"]["catalog"]["commit"] = "0" * 40
    with pytest.raises(ValidationError, match="activated snapshot"):
        CapabilityPreviewCreate.model_validate(preview)


def _content(**changes) -> dict:
    return {
        "summary": "Channel digests",
        "language": "en",
        "must_requirements": [
            {"id": "r1", "text": "Reads channels", "user_wording": "read channels"},
            {"id": "r2", "text": "Keeps notes", "user_wording": "notes", "user_facing": False},
        ],
        "usage_examples": [
            {"requirement_id": "r1", "user_sends": "/digest", "product_answers": "Latest posts"}
        ],
        "capabilities": _capabilities().model_dump(),
    } | changes


def test_a_proposed_brief_serves_only_its_own_requirements():
    content = _content()
    content["capabilities"]["capabilities"][0]["requirement_ids"] = ["r9"]
    with pytest.raises(ValidationError, match="unknown must-requirement ids: r9"):
        ProposedProductBriefContent.model_validate(content)


def test_answers_share_the_settings_cap_so_the_brief_still_fits():
    settings = [
        {"key": f"alerts.setting_{index}", "value": index, "description": "d"} for index in range(5)
    ]
    with pytest.raises(ValidationError, match="together are at most 6"):
        ProposedProductBriefContent.model_validate(_content(initial_settings=settings))


def test_a_brief_without_capabilities_keeps_its_stored_bytes():
    """Older documents echo back byte for byte: an absent field is not serialized."""
    content = _content()
    del content["capabilities"]
    dumped = ProposedProductBriefContent.model_validate(content).model_dump(mode="json")
    assert "capabilities" not in dumped


def test_the_product_projection_is_what_the_po_reads():
    projection = CapabilityPreviewProjection.model_validate(
        _preview().product.model_dump(mode="json")
    )
    text = projection.model_dump_json()
    for technical in ("tg-channels", "0.1.2", "packages/", "tg_channels.", "binding"):
        assert technical not in text
    assert isinstance(_preview().technical, CapabilityPreviewTechnical)


def _twice_selected() -> CapabilityPreviewCreate:
    """Two requests selecting the same capability: each asks its own starting-channel list."""
    preview = _preview().model_dump(mode="json", by_alias=True)
    preview["requests"].append(
        {"request_id": "more", "capability_id": CHANNELS, "wording": "more channels"}
    )
    preview["product"]["routes"].append(
        {"request_id": "more", "capability_id": CHANNELS, "route": "module", "reason": "offered"}
    )
    question = {**preview["product"]["questions"][1], "question_id": "more.q1"}
    preview["product"]["questions"].append(question | {"request_ids": ["more"]})
    preview["technical"]["targets"].append(
        {**preview["technical"]["targets"][1], "question_id": "more.q1"}
    )
    preview["technical"]["modules"].append(
        {**preview["technical"]["modules"][0], "request_id": "more"}
    )
    return CapabilityPreviewCreate.model_validate(preview)


def _twice_answered(first: list[str], second: list[str]) -> BriefCapabilities:
    capabilities = _capabilities().model_dump()
    capabilities["capabilities"].append(
        {
            "request_id": "more",
            "capability_id": CHANNELS,
            "route": "module",
            "requirement_ids": ["r1"],
        }
    )
    capabilities["answers"][1]["value"] = first
    capabilities["answers"].append(
        {"question_id": "more.q1", "kind": "text_list", "value": second, "description": "More"}
    )
    return BriefCapabilities.model_validate(capabilities)


def test_disagreeing_answers_for_one_setting_target_refuse_the_plan():
    """Durov from one request and telegram from the other: no last write wins."""
    refused = _refusal(
        capabilities=_twice_answered(["durov"], ["telegram"]), preview=_twice_selected()
    ).refusal
    assert (refused.code, refused.question_ids) == (
        CapabilityRefusalCode.SETTING_CONFLICT,
        ["channels.q1", "more.q1"],
    )
    assert "tg_channels" not in refused.model_dump_json()


def test_identical_answers_for_one_setting_target_are_one_setting():
    capabilities = _twice_answered(["durov"], ["durov"])
    plan = _derive(capabilities, _twice_selected())
    assert [(item.question_id, item.key, item.value) for item in plan.settings] == [
        ("product_language", "language", "en"),
        ("channels.q1", "tg_channels.starting_channels", ["durov"]),
    ]
    # Replay derives exactly the same plan.
    assert plan == _derive(capabilities, _twice_selected())


def test_a_corrected_answer_resolves_the_conflict():
    corrected = _twice_answered(["durov"], ["durov"])
    assert _derive(corrected, _twice_selected()).settings[1].value == ["durov"]
    only_first = _twice_answered(["durov"], ["telegram"]).model_dump()
    del only_first["answers"][2]
    plan = _derive(BriefCapabilities.model_validate(only_first), _twice_selected())
    assert [item.value for item in plan.settings] == ["en", ["durov"]]


def test_a_stored_plan_never_holds_two_values_for_one_target():
    plan = _derive().model_dump(mode="json")
    plan["settings"].append({**plan["settings"][1], "question_id": "more.q1", "value": ["x"]})
    with pytest.raises(ValidationError, match="one value per setting key and scope"):
        type(_derive()).model_validate(plan)
