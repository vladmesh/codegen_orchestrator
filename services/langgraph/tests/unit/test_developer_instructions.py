"""Tests for developer worker INSTRUCTIONS.md content."""

import re

import pytest

from scripts.platform_capabilities import load_manifest
from src.prompts import load_developer_instructions
from src.prompts.architect import SYSTEM_PROMPT as ARCHITECT_PROMPT
from src.subgraphs.devops.secret_resolver import is_computable_derived_key


def _section(content: str, heading: str) -> str:
    """One `## ` section of the instructions, whitespace-normalised."""
    body = content.split(heading, 1)[1].split("\n## ", 1)[0]
    return " ".join(body.split())


class TestDeveloperInstructions:
    """Verify INSTRUCTIONS.md contains expected curl patterns and no CLI references."""

    def setup_method(self):
        self.content = load_developer_instructions()

    def test_loads_successfully(self):
        assert self.content, "INSTRUCTIONS.md should not be empty"

    def test_missing_file_is_a_packaging_error(self, tmp_path, monkeypatch):
        from src import prompts

        monkeypatch.setattr(prompts, "PROMPTS_DIR", tmp_path)
        with pytest.raises(RuntimeError, match="developer instructions missing"):
            prompts.load_developer_instructions()

    def test_empty_file_is_a_packaging_error(self, tmp_path, monkeypatch):
        from src import prompts

        instructions_dir = tmp_path / "developer_worker"
        instructions_dir.mkdir()
        (instructions_dir / "INSTRUCTIONS.md").write_text("   \n")
        monkeypatch.setattr(prompts, "PROMPTS_DIR", tmp_path)
        with pytest.raises(RuntimeError, match="developer instructions are empty"):
            prompts.load_developer_instructions()

    def test_contains_result_reporting_endpoints(self):
        assert "localhost:9090/result" in self.content
        assert '"success":true' in self.content
        assert '"success":false' in self.content

    def test_contains_curl_commands(self):
        assert "curl -sf -X POST http://localhost:9090" in self.content

    def test_contains_infra_compose_proxy(self):
        assert "localhost:9090/infra/compose" in self.content

    def test_no_orchestrator_cli_references(self):
        assert "orchestrator dev-env" not in self.content
        assert "orchestrator project" not in self.content
        assert "orchestrator engineering" not in self.content
        assert "orchestrator deploy" not in self.content
        assert "orchestrator respond" not in self.content
        assert "orch reject" not in self.content
        assert "orch report-blocker" not in self.content

    def test_requires_env_contract_update_with_env_changes(self):
        assert "env.contract.yaml" in self.content
        assert "same commit" in self.content

    def test_uses_generated_project_test_commands(self):
        assert self.content.count("make tests") == 2
        assert "make test-integration" in self.content
        assert "make tests unit" not in self.content
        assert "make tests integration" not in self.content

    def test_requires_a_deployable_job_provider_before_reporting_success(self):
        lower = self.content.lower()
        assert "jobs_schema" in self.content
        assert 'provides: ["jobs.fire"]' in self.content
        assert "job_fired" in self.content
        assert "compose.prod.yml" in self.content
        assert "durable output" in lower
        assert "dispatch_status" in self.content
        assert "env.contract.yaml" in self.content
        assert "docker compose -f infra/compose.prod.yml config" not in self.content

    def test_a_scheduled_behaviour_is_an_in_process_timer_as_the_architect_plans_it(self):
        """The gate agrees with the Architect prompt and names no service that does not exist."""
        gate = _section(self.content, "## Scheduled Behaviour Completion Gate")
        architect = " ".join(ARCHITECT_PROMPT.split())

        assert "notifications_worker" not in self.content
        assert "in-process timer in the backend or bot that calls the declared job" in architect
        assert (
            "in-process timer in the product's own backend or bot that calls the declared job"
            in gate
        )
        assert "`FIRE JOB` stays the QA verification form, not a production scheduler" in gate
        assert "platform cannot deploy extra service modules" in gate

    def test_states_the_derived_key_rule_with_every_key_the_platform_computes(self):
        instructions = " ".join(self.content.split())
        rule = self.content.split("A `derived` contract entry", 1)[1].split("\n\n", 1)[0]
        listed = set(re.findall(r"`([A-Z_*]+)`", rule))

        assert {entry.key for entry in load_manifest().derived_keys} <= listed
        assert all(is_computable_derived_key(key) for key in listed)
        assert "PUBLIC_BASE_URL" in listed
        assert not is_computable_derived_key("UNSUPPORTED_DERIVED_URL")
        assert (
            "the platform cannot compute it; remove it, make it optional with a safe default, "
            "or use a `user_secret` if the user supplies it"
        ) in instructions
        assert "refused before any deploy" in instructions

    def test_requires_the_generated_settings_contract_before_a_seed_can_succeed(self):
        assert "POST /settings/set" in self.content
        assert "POST /settings/get" in self.content
        assert "SETTINGS_WRITE_CAPABILITY" in self.content
        assert "settings_schema" in self.content
        assert "settings_schemas.py" in self.content

    def test_uses_an_installed_packages_declared_setting_seed(self):
        lower = self.content.lower()

        assert "package-owned" in lower
        assert "declares its setting seed" in lower
        assert "db trigger" in lower
        assert "startup polling" in lower
        assert "product-owned seed code" in lower
        assert "duplicate" in lower and "service" in lower

    def test_requires_a_black_box_observable_and_a_real_provider_path_test(self):
        instructions = " ".join(self.content.lower().split())
        assert "read-only black-box observable" in instructions
        assert "seed the confirmed values" in instructions
        assert "fire the real named job contract" in instructions
        assert "exactly one durable record for each configured output partition" in instructions

    def test_states_the_kit_package_install_recipe(self):
        instructions = " ".join(self.content.split())
        assert "Package code is never hand-written into a product." in instructions
        assert ".venv/bin/kit add <name>" in instructions
        assert "resolves the kit's live catalog" in instructions
        assert "package catalog (`packages/catalog.yaml`" in instructions
        assert "Do not build a wheel yourself" in instructions
        assert "codegen_kit/_active_packages.py" in instructions
        assert "Commit everything it changed, including the wheel under" in instructions
        assert "services/backend/packages/" in instructions
        assert "uv build" not in instructions
        assert "--wheel" not in instructions
        assert "_commit" not in instructions

    def test_states_that_regeneration_is_part_of_a_manifest_or_package_change(self):
        instructions = " ".join(self.content.split())
        assert "regeneration is part of the change" in instructions
        assert "the product refuses a stale or changed generated contract" in instructions
        assert "generated package contract is stale; run make generate-from-spec" in instructions
        assert "run `make generate-from-spec` in the same change" in instructions
