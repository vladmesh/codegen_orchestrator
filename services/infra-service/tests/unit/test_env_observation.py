"""Reading a deployed service's environment answers, or says it could not ask.

The whole value of this reader is that it never guesses. A caller uses "the slot
is empty" to close out access it handed a test identity, so every way of not
getting an answer has to look different from an empty slot.
"""

import os
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
import yaml

# Set required env vars before importing modules that validate at import time.
os.environ.setdefault("API_BASE_URL", "http://localhost:8000")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
os.environ.setdefault("INTERNAL_API_KEY", "test-internal-key")

from shared.contracts.dto.server import ServerDTO, ServerStatus  # noqa: E402
from shared.contracts.queues.env_observation import (  # noqa: E402
    EnvObservationOutcome,
    EnvObservationRequest,
)
from src.provisioner.env_observation import (  # noqa: E402
    OBSERVE_PLAYBOOK,
    observe_service_env,
    parse_observation,
)

PLAYBOOK = Path(__file__).parents[2] / "ansible" / "playbooks" / OBSERVE_PLAYBOOK


def _request() -> EnvObservationRequest:
    return EnvObservationRequest(
        request_id="envobs-deploy-revoke-1",
        project_id="00000000-0000-0000-0000-000000000001",
        server_handle="vps-1",
        service_slug="palindrome-bot",
        env_key="TG_BOT_TEST_TELEGRAM_ID",
    )


def _server(public_ip: str = "203.0.113.7") -> ServerDTO:
    from datetime import UTC, datetime

    return ServerDTO(
        handle="vps-1",
        host="vps-1.example.com",
        public_ip=public_ip,
        ssh_user="deploy",
        status=ServerStatus.ACTIVE,
        is_managed=True,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )


def _ansible(success: bool, output: str):
    """Stand in for the playbook run, which is the only thing that leaves the process."""
    runner = patch("src.provisioner.env_observation.AnsibleRunner")
    server = patch(
        "src.provisioner.env_observation.get_server_info", AsyncMock(return_value=_server())
    )
    key = patch(
        "src.provisioner.env_observation.get_server_ssh_key",
        AsyncMock(return_value="-----BEGIN KEY-----"),
    )
    return runner, server, key, success, output


class TestReadingTheMarker:
    """One line out of everything Ansible says."""

    def test_the_last_reading_wins(self):
        output = (
            "TASK [Read the slot] ***\n"
            "ok: [203.0.113.7] => ENV_OBSERVATION containers=1 filled=1\n"
            'TASK [Report] ***\nok: [203.0.113.7] => {"msg": '
            '"ENV_OBSERVATION containers=2 filled=0"}\n'
        )
        assert parse_observation(output) == (2, False)

    def test_output_without_a_reading_is_not_an_empty_slot(self):
        with pytest.raises(ValueError):
            parse_observation("PLAY RECAP ***\n203.0.113.7 : ok=2 changed=0 unreachable=0\n")


class TestObservingTheService:
    @pytest.mark.asyncio
    async def test_a_filled_slot_is_reported_as_filled(self):
        runner, server, key, _, _ = _ansible(True, "")
        with runner as RunnerClass, server, key:
            RunnerClass.return_value.run_playbook.return_value = (
                True,
                "ok => ENV_OBSERVATION containers=3 filled=1",
            )
            result = await observe_service_env(_request())

        assert result.outcome is EnvObservationOutcome.OBSERVED
        assert result.present is True
        assert result.containers == 3

    @pytest.mark.asyncio
    async def test_an_empty_slot_is_read_off_the_running_containers(self):
        runner, server, key, _, _ = _ansible(True, "")
        with runner as RunnerClass, server, key:
            RunnerClass.return_value.run_playbook.return_value = (
                True,
                "ok => ENV_OBSERVATION containers=2 filled=0",
            )
            result = await observe_service_env(_request())

        assert result.outcome is EnvObservationOutcome.OBSERVED
        assert result.present is False

        call = RunnerClass.return_value.run_playbook.call_args.kwargs
        assert call["playbook_name"] == OBSERVE_PLAYBOOK
        assert call["server_ip"] == "203.0.113.7"
        assert call["extra_vars"] == {
            "service_slug": "palindrome-bot",
            "env_key": "TG_BOT_TEST_TELEGRAM_ID",
        }

    @pytest.mark.asyncio
    async def test_a_failed_playbook_is_unreachable_rather_than_empty(self):
        runner, server, key, _, _ = _ansible(True, "")
        with runner as RunnerClass, server, key:
            RunnerClass.return_value.run_playbook.return_value = (
                False,
                "STDERR: ssh: connect to host 203.0.113.7 port 22: Connection timed out",
            )
            result = await observe_service_env(_request())

        assert result.outcome is EnvObservationOutcome.UNREACHABLE
        assert result.present is None
        assert "ssh" in result.detail

    @pytest.mark.asyncio
    async def test_a_run_that_said_nothing_is_unreachable(self):
        runner, server, key, _, _ = _ansible(True, "")
        with runner as RunnerClass, server, key:
            RunnerClass.return_value.run_playbook.return_value = (True, "PLAY RECAP *** ok=1")
            result = await observe_service_env(_request())

        assert result.outcome is EnvObservationOutcome.UNREACHABLE

    @pytest.mark.asyncio
    async def test_a_service_with_nothing_running_has_no_environment_to_read(self):
        """An absent process is not an absent value; the question was never answered."""
        runner, server, key, _, _ = _ansible(True, "")
        with runner as RunnerClass, server, key:
            RunnerClass.return_value.run_playbook.return_value = (
                True,
                "ok => ENV_OBSERVATION containers=0 filled=0",
            )
            result = await observe_service_env(_request())

        assert result.outcome is EnvObservationOutcome.UNREACHABLE
        assert "no running containers" in result.detail

    @pytest.mark.asyncio
    async def test_a_server_with_no_stored_key_is_never_reached(self):
        with (
            patch("src.provisioner.env_observation.AnsibleRunner") as RunnerClass,
            patch(
                "src.provisioner.env_observation.get_server_info", AsyncMock(return_value=_server())
            ),
            patch(
                "src.provisioner.env_observation.get_server_ssh_key", AsyncMock(return_value=None)
            ),
        ):
            result = await observe_service_env(_request())

        assert result.outcome is EnvObservationOutcome.UNREACHABLE
        RunnerClass.return_value.run_playbook.assert_not_called()


class TestThePlaybookItself:
    """What the playbook reads is the containers, not the file next to them."""

    def test_it_parses_and_reads_the_running_containers(self):
        play = yaml.safe_load(PLAYBOOK.read_text())[0]
        assert play["become"] is True
        script = play["tasks"][0]["ansible.builtin.shell"]
        assert "docker compose" in play["vars"]["compose_cmd"]
        assert "ps -q" in script
        assert "docker inspect" in script
        assert "ENV_OBSERVATION containers=" in script

    def test_the_key_reaches_the_script_as_a_variable(self):
        """A key name interpolated into a shell script would be a shell fragment."""
        play = yaml.safe_load(PLAYBOOK.read_text())[0]
        task = play["tasks"][0]
        assert task["environment"]["OBSERVED_ENV_KEY"] == "{{ env_key }}"
        assert "{{ env_key }}" not in task["ansible.builtin.shell"]

    def test_it_changes_nothing(self):
        play = yaml.safe_load(PLAYBOOK.read_text())[0]
        assert play["tasks"][0]["changed_when"] is False
