"""The confirmed brief's typed settings, written into the deployed product.

The values are the ones the user confirmed, read through the released brief
endpoint and written through the product's own `settings.set`; every write is
proved by a readback, and every refusal is a bounded, credential-safe outcome
stored on the deploy run beside the run's other outcomes.
"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch
import uuid

import pytest

from shared.contracts.dto.product_brief import (
    InitialSetting,
    ProductBriefContent,
    ProductBriefRead,
    SettingScope,
)
from shared.contracts.dto.project import ProjectDTO, ProjectStatus
from shared.contracts.dto.run import RunStatus
from shared.contracts.dto.settings_seed import SettingsSeedFailureKind
from shared.contracts.queues.deploy import DeployMessage, DeployOutcome

_HANDLER_PATCH = "src.consumers.deploy_result_handler"
_CAPABILITY = "settings-capability-value"


def _project() -> ProjectDTO:
    return ProjectDTO(
        id="00000000-0000-0000-0000-000000000001",
        initiating_run_id="test-run-1",
        title="test-project",
        slug="test-project-0000",
        status=ProjectStatus.ACTIVE,
        owner_id=1,
        created_at="2026-01-01T00:00:00Z",
        updated_at="2026-01-01T00:00:00Z",
    )


def _brief(settings: list[InitialSetting] | None = None, **overrides) -> ProductBriefRead:
    base = {
        "id": "brief-1",
        "project_id": uuid.UUID("00000000-0000-0000-0000-000000000001"),
        "story_id": "story-1",
        "revision": 1,
        "title": "Reminder bot",
        "content": ProductBriefContent(
            summary="A reminder bot",
            must_requirements=[{"id": "req-1", "text": "It must remind at a chosen hour"}],
            initial_settings=(
                [InitialSetting(key="reminders.default_hour", value=9)]
                if settings is None
                else settings
            ),
        ),
        "confirmed_at": datetime.now(UTC),
        "confirmation_request_id": "confirm-1",
        "coverage_admitted_at": None,
        "planning_attempt_id": None,
        "planning_attempt_active": False,
        "planning_attempt_heartbeat_at": None,
    }
    base.update(overrides)
    return ProductBriefRead(**base)


async def _deploy(
    mock_api, *, story_id="story-1", secret_values=None, redis=None, deploy_fix_attempt=0
):
    from src.consumers.deploy_result_handler import _handle_deploy_success

    return await _handle_deploy_success(
        result={
            "deployed_url": "https://product.example.com",
            "secret_values": (
                {"SETTINGS_WRITE_CAPABILITY": _CAPABILITY}
                if secret_values is None
                else secret_values
            ),
        },
        smoke_result=None,
        task_id="deploy-1",
        project_id="proj-1",
        project=_project(),
        callback_stream="cb:1",
        telegram_chat_id="123",
        story_id=story_id,
        redis=redis or AsyncMock(),
        application_id=42,
        msg=DeployMessage(
            task_id="deploy-1",
            project_id="proj-1",
            story_id=story_id,
            telegram_chat_id="123",
            deploy_fix_attempt=deploy_fix_attempt,
        ),
    )


def _stored_result(mock_api) -> dict:
    return mock_api.patch.await_args[1]["json"]["result"]


def _stored_status(mock_api) -> str:
    return mock_api.patch.await_args[1]["json"]["status"]


class _FakeSettingsClient:
    """A stand-in for the deployed product's settings endpoints.

    It answers the way the released contract answers, so a test states what the
    product did rather than what the client returned.
    """

    instances: list["_FakeSettingsClient"] = []

    def __init__(self, base_url, *, refusals=None):
        self.base_url = base_url
        self.calls: list[tuple] = []
        self.capabilities: list[str] = []
        self.refusals = refusals or {}
        _FakeSettingsClient.instances.append(self)

    async def seed_and_resolve(self, settings, *, capability):
        from src.clients.product_settings import SettingSeedProof

        self.capabilities.append(capability)
        proofs = []
        for setting in settings:
            self.calls.append((setting.key, setting.scope, setting.subject_id, setting.value))
            failure = self.refusals.get(setting.key)
            proofs.append(SettingSeedProof(written=failure is None, failure=failure))
        return proofs


@pytest.fixture
def fake_settings_client():
    _FakeSettingsClient.instances.clear()
    refusals: dict[str, SettingsSeedFailureKind] = {}

    def factory(base_url):
        return _FakeSettingsClient(base_url, refusals=refusals)

    with patch(f"{_HANDLER_PATCH}.GeneratedServiceSettingsClient", side_effect=factory):
        yield refusals


@pytest.fixture
def mock_api():
    with patch(f"{_HANDLER_PATCH}.api_client") as api:
        api.patch = AsyncMock()
        api.get_product_brief_by_story = AsyncMock(return_value=_brief())
        yield api


class TestSeedingWhatTheUserConfirmed:
    @pytest.mark.asyncio
    async def test_confirmed_values_are_written_and_proved(self, mock_api, fake_settings_client):
        mock_api.get_product_brief_by_story = AsyncMock(
            return_value=_brief(
                [
                    InitialSetting(key="reminders.default_hour", value=9),
                    InitialSetting(
                        key="reminders.locale",
                        scope=SettingScope.USER,
                        subject_id=7,
                        value="ru",
                    ),
                ]
            )
        )

        result = await _deploy(mock_api)

        assert result["status"] == "success"
        client = _FakeSettingsClient.instances[0]
        assert client.base_url == "https://product.example.com"
        # Exactly the confirmed values, in the confirmed order.
        assert client.calls == [
            ("reminders.default_hour", SettingScope.PRODUCT, None, 9),
            ("reminders.locale", SettingScope.USER, 7, "ru"),
        ]
        # The capability comes from this deploy's resolver output and nowhere else.
        assert client.capabilities == [_CAPABILITY]
        mock_api.get_product_brief_by_story.assert_awaited_once_with("story-1")

        stored = _stored_result(mock_api)
        assert stored["deploy_outcome"] == DeployOutcome.SUCCESS.value
        assert stored["settings_seed"] == [
            {
                "key": "reminders.default_hour",
                "scope": "product",
                "subject_id": None,
                "written": True,
                "failure": None,
            },
            {
                "key": "reminders.locale",
                "scope": "user",
                "subject_id": 7,
                "written": True,
                "failure": None,
            },
        ]
        assert _CAPABILITY not in str(mock_api.patch.await_args)

    @pytest.mark.asyncio
    async def test_a_second_deploy_of_the_same_story_writes_the_same_values(
        self, mock_api, fake_settings_client
    ):
        first = await _deploy(mock_api)
        first_stored = _stored_result(mock_api)
        second = await _deploy(mock_api)

        # Same state and same stored outcome; only the wall clock moved.
        assert {k: v for k, v in first.items() if k != "finished_at"} == {
            k: v for k, v in second.items() if k != "finished_at"
        }
        assert _stored_result(mock_api) == first_stored
        assert [c.calls for c in _FakeSettingsClient.instances] == [
            [("reminders.default_hour", SettingScope.PRODUCT, None, 9)],
            [("reminders.default_hour", SettingScope.PRODUCT, None, 9)],
        ]


class TestNothingToSeed:
    @pytest.mark.asyncio
    async def test_a_story_with_no_brief_seeds_nothing(self, mock_api, fake_settings_client):
        mock_api.get_product_brief_by_story = AsyncMock(return_value=None)

        result = await _deploy(mock_api)

        assert result["status"] == "success"
        assert _FakeSettingsClient.instances == []
        assert _stored_result(mock_api)["settings_seed"] == []

    @pytest.mark.asyncio
    async def test_a_brief_with_no_settings_seeds_nothing(self, mock_api, fake_settings_client):
        mock_api.get_product_brief_by_story = AsyncMock(return_value=_brief([]))

        result = await _deploy(mock_api)

        assert result["status"] == "success"
        assert _FakeSettingsClient.instances == []
        assert _stored_result(mock_api)["settings_seed"] == []

    @pytest.mark.asyncio
    async def test_a_standalone_deploy_never_asks_for_a_brief(self, mock_api, fake_settings_client):
        result = await _deploy(mock_api, story_id="")

        assert result["status"] == "success"
        mock_api.get_product_brief_by_story.assert_not_called()
        assert _FakeSettingsClient.instances == []


class TestFailClosedAndVisibly:
    @pytest.mark.asyncio
    async def test_a_product_without_the_capability_seeds_nothing_and_says_so(
        self, mock_api, fake_settings_client
    ):
        """A confirmed setting cannot be silently skipped when its core is unavailable."""
        result = await _deploy(mock_api, secret_values={"USERS_GRANT_CAPABILITY": "other"})

        assert result["status"] == "failed"
        assert _FakeSettingsClient.instances == []
        stored = _stored_result(mock_api)
        assert stored["deploy_outcome"] == DeployOutcome.SETTINGS_SEED_FAILED.value
        assert stored["settings_seed"] == [
            {
                "key": "reminders.default_hour",
                "scope": "product",
                "subject_id": None,
                "written": False,
                "failure": SettingsSeedFailureKind.CAPABILITY_UNAVAILABLE.value,
            }
        ]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "failure",
        [
            failure
            for failure in SettingsSeedFailureKind
            if failure is not SettingsSeedFailureKind.CAPABILITY_UNAVAILABLE
        ],
    )
    async def test_every_unproved_confirmed_setting_holds_the_deploy_back(
        self, mock_api, fake_settings_client, failure
    ):
        """No current failure kind may turn a confirmed setting into deploy success."""
        fake_settings_client["reminders.default_hour"] = failure
        result = await _deploy(mock_api)

        assert result["status"] == "failed"
        assert _stored_status(mock_api) == RunStatus.FAILED.value
        stored = _stored_result(mock_api)
        # Its own outcome, not the owner-grant one: the supervisor reconciles
        # that one to SUCCESS once the grant is applied, which would launder a
        # setting the product never accepted into a deploy handed to QA.
        assert stored["deploy_outcome"] == DeployOutcome.SETTINGS_SEED_FAILED.value
        assert stored["error_details"] == f"settings_seed:{failure.value}"
        # The record of what did and did not reach the product survives the refusal.
        assert stored["settings_seed"] == [
            {
                "key": "reminders.default_hour",
                "scope": "product",
                "subject_id": None,
                "written": False,
                "failure": failure.value,
            }
        ]
        assert _CAPABILITY not in str(mock_api.patch.await_args)

    @pytest.mark.asyncio
    async def test_the_settings_that_did_reach_the_product_are_recorded_too(
        self, mock_api, fake_settings_client
    ):
        mock_api.get_product_brief_by_story = AsyncMock(
            return_value=_brief(
                [
                    InitialSetting(key="reminders.default_hour", value=9),
                    InitialSetting(key="reminders.locale", value="ru"),
                ]
            )
        )
        fake_settings_client["reminders.locale"] = SettingsSeedFailureKind.KEY_NOT_DECLARED

        result = await _deploy(mock_api)

        assert result["status"] == "failed"
        stored = _stored_result(mock_api)
        assert stored["deploy_outcome"] == DeployOutcome.SETTINGS_SEED_FAILED.value
        assert [(s["key"], s["written"], s["failure"]) for s in stored["settings_seed"]] == [
            ("reminders.default_hour", True, None),
            ("reminders.locale", False, SettingsSeedFailureKind.KEY_NOT_DECLARED.value),
        ]

    @pytest.mark.asyncio
    async def test_a_seed_failure_preserves_the_repair_attempt(
        self, mock_api, fake_settings_client
    ):
        """A repaired deploy cannot reset the supervisor's bounded-fix counter."""
        fake_settings_client["reminders.default_hour"] = SettingsSeedFailureKind.KEY_NOT_DECLARED

        result = await _deploy(mock_api, deploy_fix_attempt=1)

        assert result["status"] == "failed"
        assert _stored_result(mock_api)["deploy_fix_attempt"] == 1

    @pytest.mark.asyncio
    async def test_mixed_seed_failures_persist_one_complete_stable_detail(
        self, mock_api, fake_settings_client
    ):
        mock_api.get_product_brief_by_story = AsyncMock(
            return_value=_brief(
                [
                    InitialSetting(key="reminders.default_hour", value=9),
                    InitialSetting(key="reminders.locale", value="ru"),
                ]
            )
        )
        fake_settings_client["reminders.default_hour"] = SettingsSeedFailureKind.TRANSPORT
        fake_settings_client["reminders.locale"] = SettingsSeedFailureKind.KEY_NOT_DECLARED

        result = await _deploy(mock_api)

        assert result["status"] == "failed"
        stored = _stored_result(mock_api)
        assert stored["error_details"] == "settings_seed:key_not_declared,transport"
        assert "key_not_declared,transport" in result["error"]
