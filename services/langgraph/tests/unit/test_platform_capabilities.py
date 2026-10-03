"""The platform capability manifest against the code it describes.

Every expectation here is read off the resolver's, the port roles' and the env contract's
own constants, never a list typed out in this file: a derived key, a port service or a
secret kind the code gains fails these tests, naming it, until the manifest lists it.
"""

from __future__ import annotations

import json
import re
from typing import get_args

from langchain_core.messages import SystemMessage
import pytest
import yaml

from scripts.platform_capabilities import (
    ARCHITECT_BLOCK_HEADING,
    ARCHITECT_BLOCK_PATH,
    DOCUMENT_PATH,
    PROMPT_BLOCK_HEADING,
    PROMPT_BLOCK_PATH,
    RUNTIME_PATH,
    CapabilityManifest,
    load_manifest,
    render_architect_block,
    render_document,
    render_prompt_block,
    render_runtime,
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
from src.prompts.platform_capabilities import (
    ARCHITECT_PLATFORM_CAPABILITIES_PROMPT,
    PLATFORM_CAPABILITIES_PROMPT,
)
from src.prompts.po import SYSTEM_PROMPT as PO_SYSTEM_PROMPT
from src.subgraphs.devops.secret_resolver import (
    CONTEXT_DERIVED_SECRETS,
    IMAGE_KEY_SUFFIX,
    SecretResolverNode,
    is_computable_derived_key,
)

#: The compact block is budgeted on its own, beside the PO's capped `SYSTEM_PROMPT`.
PROMPT_BLOCK_BUDGET = 5000
#: The Architect's technical block, budgeted on its own beside the Architect prompt.
ARCHITECT_BLOCK_BUDGET = 7000
#: Technical detail the PO's product block must never carry.
TECHNICAL_TERMS = ("derived", "POSTGRES", "http://", "server IP", "settings v1", "jobs", "kit")
#: The v4 ids merged into `web_presence`; briefs may have recorded them in `variant_choices`.
MERGED_V4_IDS = {
    "https_domain",
    "custom_domain",
    "telegram_mini_app",
    "web_frontend",
}
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
    return is_computable_derived_key(key)


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


def _pinned_kit_commit() -> str:
    """The commit the vendored render of the pinned release resolves the kit tooling to."""
    repository = TEMPLATE_PIN.source.rsplit("/", 1)[-1]
    project = (TEMPLATE_PIN.fixture_path() / "pyproject.toml").read_text()
    [commit] = re.findall(rf"{re.escape(repository)}\.git@([0-9a-f]{{40}})", project)
    return commit


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

        assert (kit.source, kit.commit) == (TEMPLATE_PIN.source, _pinned_kit_commit())

    def test_every_derived_key_the_pinned_kit_deploys_is_computed_or_named_as_not(self):
        not_resolved = {entry.key for entry in load_manifest().kit_derived_keys_not_resolved}
        declared = _pinned_kit_production_derived_keys()

        unaccounted = sorted(
            key for key in declared if not _resolver_computes(key) and key not in not_resolved
        )
        stale = sorted(key for key in not_resolved if _resolver_computes(key))

        assert (unaccounted, stale) == ([], [])


class TestTheManifestIsVersionedAndReviewed:
    def test_it_carries_a_version_and_the_owner_review_marker(self):
        manifest = load_manifest()

        assert manifest.version == 13
        assert manifest.status == "owner-reviewed"
        assert manifest.review == "product list agreed by the owner 2026-09-28"

    def test_the_document_shows_both_at_the_top(self):
        manifest = load_manifest()
        head = "\n".join(DOCUMENT_PATH.read_text().splitlines()[:6])

        assert f"Version {manifest.version}" in head
        assert "status: owner-reviewed (product list agreed by the owner 2026-09-28)" in head

    def test_every_limitation_says_why_for_the_product_and_for_the_code(self):
        assert all(item.why.strip() and item.technical.strip() for item in load_manifest().cannot)

    def test_the_document_puts_the_product_part_first(self):
        document = DOCUMENT_PATH.read_text()

        assert document.index("## For the product owner") < document.index("## Technical detail")

    def test_merged_ids_are_aliases_only(self):
        items = load_manifest().cannot
        ids = {item.id for item in items}
        aliases = [alias for item in items for alias in item.aliases]

        assert set({item.id: item.aliases for item in items}["web_presence"]) == MERGED_V4_IDS
        assert len(aliases) == len(set(aliases))
        assert ids.isdisjoint(aliases)


class TestTheRenderingsAreCurrent:
    """Run `python -m scripts.platform_capabilities` when one of these fails."""

    def test_the_document_is_rendered_from_the_manifest(self):
        assert DOCUMENT_PATH.read_text() == render_document(load_manifest())

    def test_the_prompt_block_is_rendered_from_the_manifest(self):
        assert PROMPT_BLOCK_PATH.read_text() == render_prompt_block(load_manifest())

    def test_the_architect_block_is_rendered_from_the_manifest(self):
        assert ARCHITECT_BLOCK_PATH.read_text() == render_architect_block(load_manifest())

    def test_the_runtime_data_is_rendered_from_the_manifest(self):
        assert RUNTIME_PATH.read_text() == render_runtime(load_manifest())

    def test_every_limitation_has_unique_id_and_lowercase_english_and_russian_terms(self):
        items = load_manifest().cannot
        assert len({item.id for item in items}) == len(items)
        for item in items:
            assert all(term == term.lower() and term.strip() for term in item.detect)
            assert any(re.search("[а-я]", term) for term in item.detect)
            assert any(re.search("[a-z]", term) for term in item.detect)


class TestTheCompactBlock:
    def test_it_stays_within_its_own_budget(self):
        assert len(PLATFORM_CAPABILITIES_PROMPT) <= PROMPT_BLOCK_BUDGET

    def test_it_carries_can_cannot_workarounds_and_the_version(self):
        manifest = load_manifest()
        block = PLATFORM_CAPABILITIES_PROMPT

        assert block.startswith(
            f"{PROMPT_BLOCK_HEADING} (manifest v{manifest.version}, owner-reviewed)"
        )
        assert all(f"- {item.name}: " in block for item in manifest.can)
        assert all(f"- {item.name}: [{item.id}] " in block for item in manifest.cannot)
        assert all(
            " ".join(item.workaround.split()).removesuffix(".") in block
            for item in manifest.cannot
            if item.workaround
        )

    @pytest.mark.parametrize("term", TECHNICAL_TERMS)
    def test_it_carries_no_technical_detail(self, term):
        assert term.casefold() not in PLATFORM_CAPABILITIES_PROMPT.casefold()

    def test_the_runtime_data_carries_no_technical_field(self):
        runtime = json.loads(RUNTIME_PATH.read_text())

        assert all("technical" not in item for item in runtime["cannot"])


class TestTheArchitectBlock:
    def test_it_stays_within_its_own_budget(self):
        assert len(ARCHITECT_PLATFORM_CAPABILITIES_PROMPT) <= ARCHITECT_BLOCK_BUDGET

    def test_it_carries_the_technical_detail(self):
        manifest = load_manifest()
        block = ARCHITECT_PLATFORM_CAPABILITIES_PROMPT

        assert block.startswith(f"{ARCHITECT_BLOCK_HEADING} (manifest v{manifest.version}, ")
        assert all(" ".join(item.how.split()) in block for item in manifest.can)
        assert all(" ".join(item.technical.split()) in block for item in manifest.cannot)
        assert "Derived keys (no others exist): " in block

    def test_it_states_that_the_core_timer_fires_reminders_tick_in_production(self):
        block = " ".join(ARCHITECT_PLATFORM_CAPABILITIES_PROMPT.split())

        assert (
            "The kit core timer loop fires each timer an installed package declares, "
            "in production with no caller: reminders `reminders.tick` every 60 s."
        ) in block
        assert "nothing calls the `reminders` package's `reminders.tick`" not in block
        assert "must run its own timer loop" not in block

    def test_it_states_the_identity_bearing_package_api(self):
        block = " ".join(ARCHITECT_PLATFORM_CAPABILITIES_PROMPT.split())

        for header in ("X-Identity-Capability", "X-User-Channel", "X-User-External-Id"):
            assert f"`{header}`" in block
        assert "`USER_IDENTITY_CAPABILITY`" in block
        assert "`user_ref` is `<channel>:<external_id>`" in block

    def test_it_names_the_package_catalog(self):
        block = " ".join(ARCHITECT_PLATFORM_CAPABILITIES_PROMPT.split())

        assert "`packages/catalog.yaml`" in block
        assert "install with `kit add <name>`" in block

    def test_it_lists_no_package_of_its_own(self):
        """Packages come from the kit's live catalog at planning time, not from here.

        A static list would make a package release wait for an orchestrator edit, and
        would go on offering a version the kit no longer releases.
        """
        block = " ".join(ARCHITECT_PLATFORM_CAPABILITIES_PROMPT.split())

        assert "read live from the kit's default branch at planning time" in block
        assert "a new package release needs no orchestrator change" in block
        assert "reminders 0.4.0" not in block
        assert not hasattr(load_manifest().kit, "packages")


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

    def test_the_architect_plans_with_the_technical_block(self):
        assert ARCHITECT_PLATFORM_CAPABILITIES_PROMPT in ARCHITECT_PROMPT
        assert PLATFORM_CAPABILITIES_PROMPT not in ARCHITECT_PROMPT

    def test_the_po_does_not_read_the_technical_block(self):
        [system] = po_prompt({"messages": []}, {"configurable": {}})

        assert ARCHITECT_PLATFORM_CAPABILITIES_PROMPT not in system.content
