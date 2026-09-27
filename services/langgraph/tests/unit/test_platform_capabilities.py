"""The platform capability manifest against the code it describes.

Every expectation here is read off the resolver's, the port roles' and the env contract's
own constants, never a list typed out in this file: a derived key, a port service or a
secret kind the code gains fails these tests, naming it, until the manifest lists it.
"""

from __future__ import annotations

from typing import get_args

from langchain_core.messages import SystemMessage
import yaml

from scripts.platform_capabilities import (
    DOCUMENT_PATH,
    PROMPT_BLOCK_HEADING,
    PROMPT_BLOCK_PATH,
    CapabilityManifest,
    load_manifest,
    render_document,
    render_prompt_block,
)
from scripts.template_pin import TEMPLATE_PIN
from shared.contracts.dto.project import REQUESTABLE_SERVICE_MODULES
from shared.contracts.env_contract import EnvSource
from shared.contracts.service_ports import (
    DEPLOY_INFRA_PORT_SERVICES,
    SERVICE_MODULE_PORT_ROLES,
    is_http_health_port_service,
)
from src.agents.po.graph import po_prompt
from src.agents.po.situation import SITUATION_CONFIG_KEY
from src.prompts.architect import SYSTEM_PROMPT as ARCHITECT_PROMPT
from src.prompts.platform_capabilities import PLATFORM_CAPABILITIES_PROMPT
from src.prompts.po import SYSTEM_PROMPT as PO_SYSTEM_PROMPT
from src.subgraphs.devops.secret_resolver import (
    CONTEXT_DERIVED_SECRETS,
    IMAGE_KEY_SUFFIX,
    SecretResolverNode,
)

#: The compact block is budgeted on its own, beside the PO's capped `SYSTEM_PROMPT`.
PROMPT_BLOCK_BUDGET = 5000
#: How the manifest spells the `*_IMAGE` family the resolver computes by suffix.
IMAGE_FAMILY = f"*{IMAGE_KEY_SUFFIX}"
#: The capped PO prompt, from `test_po_prompts.py`; the block must not count against it.
PO_SYSTEM_PROMPT_CAP = 16000


def _derived_keys_the_resolver_computes() -> set[str]:
    return {
        *SecretResolverNode._STATIC_SECRETS,
        *CONTEXT_DERIVED_SECRETS,
        *SecretResolverNode._PORT_SERVICE_MAP,
        IMAGE_FAMILY,
    }


def _resolver_computes(key: str) -> bool:
    return key in _derived_keys_the_resolver_computes() or key.endswith(IMAGE_KEY_SUFFIX)


def _port_services_the_code_supports() -> set[str]:
    return {
        *(module.value for module in SERVICE_MODULE_PORT_ROLES),
        *DEPLOY_INFRA_PORT_SERVICES,
        *SecretResolverNode._PORT_SERVICE_MAP.values(),
    }


def _gaps(kind: str, expected: set[str], listed: set[str]) -> list[str]:
    """One line per entry the code has and the manifest lacks, or the other way round."""
    return [f"{kind} missing from the manifest: {name}" for name in sorted(expected - listed)] + [
        f"{kind} in the manifest but not in the code: {name}" for name in sorted(listed - expected)
    ]


def coverage_gaps(manifest: CapabilityManifest) -> list[str]:
    """Everything the code supports that the manifest does not list, and the reverse."""
    return [
        *_gaps(
            "derived key",
            _derived_keys_the_resolver_computes(),
            {entry.key for entry in manifest.derived_keys},
        ),
        *_gaps(
            "deploy target",
            _port_services_the_code_supports(),
            {target.service for target in manifest.deploy_targets},
        ),
        *_gaps(
            "secret kind",
            set(get_args(EnvSource)),
            {kind.source for kind in manifest.secret_kinds},
        ),
        *_gaps(
            "kit module",
            {module.value for module in REQUESTABLE_SERVICE_MODULES},
            {module.name for module in manifest.kit.modules},
        ),
    ]


def _pinned_kit_production_derived_keys() -> set[str]:
    keys = set()
    for path in TEMPLATE_PIN.fixture_path().rglob("env.contract.yaml"):
        entries = yaml.safe_load(path.read_text())["entries"]
        keys |= {
            key
            for key, entry in entries.items()
            if entry["source"] == "derived" and "production" in entry["environments"]
        }
    return keys


class TestTheManifestCoversTheCode:
    def test_every_derived_key_port_service_secret_kind_and_module_is_listed(self):
        assert coverage_gaps(load_manifest()) == []

    def test_a_derived_key_missing_from_the_manifest_is_named(self):
        manifest = load_manifest()
        without = manifest.model_copy(
            update={"derived_keys": [e for e in manifest.derived_keys if e.key != "POSTGRES_DB"]}
        )

        assert coverage_gaps(without) == ["derived key missing from the manifest: POSTGRES_DB"]

    def test_the_image_family_missing_from_the_manifest_is_named(self):
        manifest = load_manifest()
        without = manifest.model_copy(
            update={"derived_keys": [e for e in manifest.derived_keys if e.key != IMAGE_FAMILY]}
        )

        assert coverage_gaps(without) == [f"derived key missing from the manifest: {IMAGE_FAMILY}"]

    def test_a_deploy_target_missing_from_the_manifest_is_named(self):
        manifest = load_manifest()
        without = manifest.model_copy(
            update={"deploy_targets": [t for t in manifest.deploy_targets if t.service != "redis"]}
        )

        assert coverage_gaps(without) == ["deploy target missing from the manifest: redis"]

    def test_static_derived_values_are_the_resolver_values(self):
        listed = {entry.key: entry.value for entry in load_manifest().derived_keys}

        assert {
            key: listed[key] for key in SecretResolverNode._STATIC_SECRETS
        } == SecretResolverNode._STATIC_SECRETS

    def test_deploy_target_roles_match_the_port_roles(self):
        requestable = {module.value for module in REQUESTABLE_SERVICE_MODULES}
        wrong = [
            target.service
            for target in load_manifest().deploy_targets
            if target.http_health != is_http_health_port_service(target.service)
            or target.requestable != (target.service in requestable)
        ]

        assert wrong == []

    def test_the_kit_is_the_pinned_template(self):
        kit = load_manifest().derived_from.kit

        assert (kit.source, kit.ref) == (TEMPLATE_PIN.source, TEMPLATE_PIN.ref)

    def test_every_derived_key_the_pinned_kit_deploys_is_computed_or_named_as_not(self):
        not_resolved = {entry.key for entry in load_manifest().kit_derived_keys_not_resolved}
        declared = _pinned_kit_production_derived_keys()

        unaccounted = sorted(
            key for key in declared if not _resolver_computes(key) and key not in not_resolved
        )
        stale = sorted(key for key in not_resolved if _resolver_computes(key))

        assert (unaccounted, stale) == ([], [])


class TestTheManifestIsVersionedAndDrafted:
    def test_it_carries_a_version_and_the_owner_read_through_marker(self):
        manifest = load_manifest()

        assert manifest.version >= 1
        assert manifest.status == "draft"
        assert manifest.review == "owner read-through pending"

    def test_the_document_shows_both_at_the_top(self):
        manifest = load_manifest()
        head = "\n".join(DOCUMENT_PATH.read_text().splitlines()[:6])

        assert f"Version {manifest.version}" in head
        assert "status: draft (owner read-through pending)" in head

    def test_every_limitation_says_why(self):
        assert all(item.why.strip() for item in load_manifest().cannot)


class TestTheRenderingsAreCurrent:
    """Run `python -m scripts.platform_capabilities` when one of these fails."""

    def test_the_document_is_rendered_from_the_manifest(self):
        assert DOCUMENT_PATH.read_text() == render_document(load_manifest())

    def test_the_prompt_block_is_rendered_from_the_manifest(self):
        assert PROMPT_BLOCK_PATH.read_text() == render_prompt_block(load_manifest())


class TestTheCompactBlock:
    def test_it_stays_within_its_own_budget(self):
        assert len(PLATFORM_CAPABILITIES_PROMPT) <= PROMPT_BLOCK_BUDGET

    def test_it_carries_can_cannot_workarounds_and_the_version(self):
        manifest = load_manifest()
        block = PLATFORM_CAPABILITIES_PROMPT

        assert block.startswith(f"{PROMPT_BLOCK_HEADING} (manifest v{manifest.version}, draft)")
        assert all(f"- {item.name}: " in block for item in manifest.can)
        assert all(f"- {item.name}: " in block for item in manifest.cannot)
        assert all(
            " ".join(item.workaround.split()) in block
            for item in manifest.cannot
            if item.workaround
        )


class TestThePromptsCarryTheBlock:
    def test_the_po_reads_it_on_every_turn(self):
        plain = po_prompt({"messages": []}, {"configurable": {}})
        with_situation = po_prompt(
            {"messages": []}, {"configurable": {SITUATION_CONFIG_KEY: "## Situation"}}
        )

        for messages in (plain, with_situation):
            [system] = messages
            assert isinstance(system, SystemMessage)
            assert PLATFORM_CAPABILITIES_PROMPT in system.content

    def test_the_block_is_not_counted_in_the_capped_po_prompt(self):
        assert PLATFORM_CAPABILITIES_PROMPT not in PO_SYSTEM_PROMPT
        assert len(PO_SYSTEM_PROMPT) < PO_SYSTEM_PROMPT_CAP

    def test_the_po_rule_points_at_the_block(self):
        assert f"`{PROMPT_BLOCK_HEADING}`" in PO_SYSTEM_PROMPT

    def test_the_architect_plans_with_it(self):
        assert PLATFORM_CAPABILITIES_PROMPT in ARCHITECT_PROMPT
