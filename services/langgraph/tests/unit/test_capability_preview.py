"""The Architect's capability preview over the activated catalog, from genuine bytes.

`resolve_preview` is pure: the catalog (`activated_kit_catalog`, the activated commit's
catalog and the published tg-channels 0.1.2 / reminders 0.5.0 resources), the project's
shape and the rollout decision go in, and a preview the API stores comes out, or a typed
refusal. Nothing here mocks the resolver, the kit loaders or the closure selection.
"""

from __future__ import annotations

from dataclasses import replace
import uuid

import pytest

from shared.catalog_activation import CATALOG_ACTIVATION
from shared.contracts.dto.capability_preview import (
    CapabilityRequest,
    CapabilityRoute,
    PreviewRefusalCode,
    QuestionKind,
    RouteReason,
)
from src.capability_feasibility import platform_cannot
from src.capability_preview import PreviewRefused, capability_id, capability_offers, resolve_preview
from src.kit_catalog import KitCatalogFailure, KitCatalogUnavailable

PROJECT = uuid.UUID("7e1b5b1e-2a51-4e0e-9a59-16b39c6c3f01")
CHANNELS = capability_id("tg-channels")
REMINDERS = capability_id("reminders")
BOT = {"backend", "tg_bot"}


def _requests(*items: dict) -> list[CapabilityRequest]:
    return [CapabilityRequest.model_validate(item) for item in items]


def _preview(catalog, requests, *, modules=BOT, admits=True):
    return resolve_preview(
        project_id=PROJECT,
        requests=requests,
        catalog=catalog,
        activation=CATALOG_ACTIVATION,
        project_modules=set(modules),
        rollout_admits=admits,
        platform_cannot=platform_cannot,
    )


CHANNEL_REQUEST = {"request_id": "channels", "capability_id": CHANNELS, "wording": "Кипр каналы"}


def test_offers_are_user_level_capabilities_with_opaque_ids(activated_kit_catalog):
    offers = capability_offers(activated_kit_catalog)

    assert [offer.capability_id for offer in offers] == [REMINDERS, CHANNELS]
    channels = offers[1]
    assert "read public Telegram channels" in channels.phrases
    assert "читать публичные Telegram-каналы" in channels.phrases
    rendered = " ".join(offer.model_dump_json() for offer in offers)
    for technical in ("tg-channels", "0.1.2", "0.5.0", "packages/", "codegen-kit", "textparse"):
        assert technical not in rendered


def test_a_public_channel_capability_resolves_to_the_published_closure(activated_kit_catalog):
    preview = _preview(activated_kit_catalog, _requests(CHANNEL_REQUEST))

    [route] = preview.product.routes
    assert (route.route, route.reason) == (CapabilityRoute.MODULE, RouteReason.OFFERED)
    [module] = preview.technical.modules
    install = module.install
    assert (install.package.name, install.package.version, install.package.tag) == (
        "tg-channels",
        "0.1.2",
        "packages/tg-channels/v0.1.2",
    )
    assert install.libraries == []
    assert install.binding.resource == "codegen_kit_tg_channels:bindings/default.yaml"
    assert install.catalog == CATALOG_ACTIVATION.source()
    assert install.catalog_digest == CATALOG_ACTIVATION.catalog_digest
    assert install.core_version == CATALOG_ACTIVATION.core_version == "2.5.0"
    assert preview.technical.activation == CATALOG_ACTIVATION


def test_its_questions_are_an_explicit_language_and_starting_channels(activated_kit_catalog):
    preview = _preview(activated_kit_catalog, _requests(CHANNEL_REQUEST))

    language, channels = preview.product.questions
    assert (language.question_id, language.kind, language.required, language.choices) == (
        "product_language",
        QuestionKind.PRODUCT_LANGUAGE,
        True,
        ["ru", "en"],
    )
    assert (channels.kind, channels.required, channels.max_items) == (
        QuestionKind.TEXT_LIST,
        False,
        50,
    )
    targets = {target.question_id: target for target in preview.technical.targets}
    assert (targets["product_language"].key, targets["product_language"].scope) == (
        "language",
        "product",
    )
    assert targets[channels.question_id].key == "tg_channels.starting_channels"
    assert targets[channels.question_id].unique_items is True


def test_its_limitations_are_the_platform_quota(activated_kit_catalog):
    preview = _preview(activated_kit_catalog, _requests(CHANNEL_REQUEST))

    assert {(item.name, item.value) for item in preview.product.limitations} == {
        ("channels_max", 50),
        ("requests_per_minute", 60),
        ("resolve_per_day", 200),
    }


def test_the_product_projection_carries_no_technical_fact(activated_kit_catalog):
    preview = _preview(
        activated_kit_catalog,
        _requests(
            CHANNEL_REQUEST,
            {"request_id": "remind", "capability_id": REMINDERS, "wording": "remind me"},
        ),
    )
    text = preview.product.model_dump_json()
    for technical in (
        "tg-channels",
        "tg_channels",
        "codegen",
        "0.1.2",
        "0.5.0",
        "textparse",
        "packages/",
        "binding",
        CATALOG_ACTIVATION.commit,
    ):
        assert technical not in text


def test_more_than_the_offer_is_a_module_with_glue(activated_kit_catalog):
    request = CHANNEL_REQUEST | {"beyond": "translate every post to Greek"}
    [route] = _preview(activated_kit_catalog, _requests(request)).product.routes
    assert (route.route, route.reason) == (
        CapabilityRoute.MODULE_WITH_GLUE,
        RouteReason.BEYOND_OFFER,
    )


def test_a_reminder_module_asks_for_its_product_timezone(activated_kit_catalog):
    preview = _preview(
        activated_kit_catalog,
        _requests({"request_id": "remind", "capability_id": REMINDERS, "wording": "remind me"}),
    )
    [timezone] = preview.product.questions
    assert (timezone.question_id, timezone.kind, timezone.required) == (
        "product_timezone",
        QuestionKind.TIMEZONE,
        True,
    )
    [module] = preview.technical.modules
    assert [library.name for library in module.install.libraries] == ["textparse"]


@pytest.mark.parametrize(
    "capability,route",
    [(CHANNELS, CapabilityRoute.IMPOSSIBLE), (REMINDERS, CapabilityRoute.FROM_SCRATCH)],
    ids=["platform-sourced", "programmable"],
)
def test_outside_the_rollout_the_route_is_honest(activated_kit_catalog, capability, route):
    """No module is installed; only what the platform alone supplies becomes impossible."""
    request = {"request_id": "x", "capability_id": capability, "wording": "wanted"}
    preview = _preview(activated_kit_catalog, _requests(request), admits=False)

    [decided] = preview.product.routes
    assert (decided.route, decided.reason) == (route, RouteReason.ROLLOUT_NOT_ENABLED)
    assert preview.technical.modules == [] and preview.product.questions == []


def test_a_project_that_is_not_a_bot_gets_no_module(activated_kit_catalog):
    preview = _preview(activated_kit_catalog, _requests(CHANNEL_REQUEST), modules={"backend"})
    [route] = preview.product.routes
    assert (route.route, route.reason) == (CapabilityRoute.IMPOSSIBLE, RouteReason.PRODUCT_SHAPE)


def test_requests_beyond_the_catalog_are_programmable_unless_the_platform_cannot(
    activated_kit_catalog,
):
    preview = _preview(
        activated_kit_catalog,
        _requests(
            {"request_id": "notes", "wording": "keep a list of my notes"},
            {"request_id": "pay", "wording": "accept card payments for orders"},
        ),
    )
    routes = {route.request_id: (route.route, route.reason) for route in preview.product.routes}
    assert routes == {
        "notes": (CapabilityRoute.FROM_SCRATCH, RouteReason.NOT_OFFERED),
        "pay": (CapabilityRoute.IMPOSSIBLE, RouteReason.PLATFORM_CANNOT),
    }


def test_the_same_inputs_give_the_same_preview(activated_kit_catalog):
    requests = _requests(CHANNEL_REQUEST)
    first = _preview(activated_kit_catalog, requests).model_dump(mode="json", by_alias=True)
    second = _preview(activated_kit_catalog, requests).model_dump(mode="json", by_alias=True)
    assert first == second


def test_an_id_the_catalog_does_not_offer_is_refused(activated_kit_catalog):
    request = CHANNEL_REQUEST | {"capability_id": "cap-000000000000"}
    with pytest.raises(PreviewRefused) as refused:
        _preview(activated_kit_catalog, _requests(request))
    assert refused.value.refusal.code is PreviewRefusalCode.UNKNOWN_CAPABILITY
    assert refused.value.refusal.request_ids == ["channels"]


@pytest.mark.parametrize(
    "failure,code",
    [
        (KitCatalogFailure.TRANSPORT, PreviewRefusalCode.CATALOG_UNAVAILABLE),
        (KitCatalogFailure.PROVENANCE, PreviewRefusalCode.CATALOG_INACTIVE),
        (KitCatalogFailure.INACTIVE, PreviewRefusalCode.CATALOG_INACTIVE),
    ],
)
def test_without_the_activated_catalog_no_module_is_promised(failure, code):
    unavailable = KitCatalogUnavailable("https://kit.invalid", failure, "detail")
    with pytest.raises(PreviewRefused) as refused:
        _preview(unavailable, _requests(CHANNEL_REQUEST))
    assert refused.value.refusal.code is code


def test_without_the_catalog_ordinary_requests_are_still_previewed():
    unavailable = KitCatalogUnavailable("https://kit.invalid", KitCatalogFailure.TRANSPORT, "")
    preview = _preview(unavailable, _requests({"request_id": "notes", "wording": "notes"}))
    assert preview.product.routes[0].route is CapabilityRoute.FROM_SCRATCH


def test_a_module_that_no_longer_installs_is_refused_before_the_user_is_asked(
    activated_kit_catalog,
):
    broken = replace(activated_kit_catalog, bindings={})
    with pytest.raises(PreviewRefused) as refused:
        _preview(broken, _requests(CHANNEL_REQUEST))
    assert refused.value.refusal.code is PreviewRefusalCode.CAPABILITY_UNRESOLVABLE


def test_a_required_setting_the_preview_cannot_ask_is_refused(activated_kit_catalog):
    manifest = activated_kit_catalog.manifests["tg-channels"].replace(
        "setting_seeds:\n  - key: starting_channels\n    scope: product\n", "setting_seeds: []\n"
    )
    manifest = manifest.replace(
        "    starting_channels:\n      type: array",
        "    starting_channels:\n      minItems: 1\n      type: array",
    )
    broken = replace(
        activated_kit_catalog,
        manifests=activated_kit_catalog.manifests | {"tg-channels": manifest},
    )
    with pytest.raises(PreviewRefused) as refused:
        _preview(broken, _requests(CHANNEL_REQUEST))
    assert refused.value.refusal.code is PreviewRefusalCode.UNSUPPORTED_QUESTION
