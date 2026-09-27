"""A required derived key the platform cannot compute fails engineering, not the deploy.

story-92b433c8 declared a required `derived` `PUBLIC_BASE_URL` in its backend
contract, and it surfaced only as `Unknown computed secret: PUBLIC_BASE_URL` in
the secret resolver, after three tasks and three deploys. The engineering
success path now reads the contract at the commit, with the deploy's own loader,
and fails the attempt through the ordinary failure path before any deploy.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml

from scripts.template_pin import TEMPLATE_PIN
from shared.contracts.dto.engineering import EngineeringStatus
from shared.contracts.dto.run import RunStatus
from shared.contracts.dto.run_result import EngineeringFailureReason, EngineeringRunResult
from shared.queues import DEPLOY_QUEUE
from src.consumers import engineering_result_handler
from src.consumers.engineering import EngineeringSuccessParams
from src.subgraphs.devops import env_contract_loader
from tests.unit.factories import make_project, make_repository

COMMIT = "c0ffee0000000000000000000000000000000000"
TASK_ID = "eng-1"
PLANNING_TASK_ID = "task-42"
REASON = (
    "the platform cannot compute PUBLIC_BASE_URL; remove it, make it optional with a safe "
    "default, or use a user_secret if the user supplies it; see the capability manifest's "
    "derived keys"
)


def _kit_fragments() -> dict[str, str]:
    """The pinned kit's contract fragments, by repository path."""
    root = TEMPLATE_PIN.fixture_path()
    return {
        str(path.relative_to(root)): path.read_text()
        for path in sorted(Path(root).rglob("env.contract.yaml"))
    }


def _with_backend_entry(key: str, entry: dict) -> dict[str, str]:
    fragments = _kit_fragments()
    backend = yaml.safe_load(fragments["services/backend/env.contract.yaml"])
    backend["entries"][key] = entry
    fragments["services/backend/env.contract.yaml"] = yaml.safe_dump(backend)
    return fragments


def _derived(*, required: bool, environments=("local", "production")) -> dict:
    return {
        "source": "derived",
        "environments": list(environments),
        "consumers": ["backend"],
        "required": required,
    }


class _GitHub:
    """The repository at one commit, as the GitHub App client reads it."""

    def __init__(self, files: dict[str, str]):
        self.files = files
        self.refs: set[str] = set()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return None

    async def list_repo_files_recursive(self, owner, repo, ref):
        assert (owner, repo) == ("org", "product")
        self.refs.add(ref)
        return [*self.files, "README.md"]

    async def get_file_contents(self, owner, repo, path, ref):
        self.refs.add(ref)
        return self.files.get(path)


@pytest.fixture
def mock_redis():
    r = AsyncMock()
    r.redis = AsyncMock()
    r.publish_message = AsyncMock()
    r.publish_flat = AsyncMock()
    return r


@pytest.fixture
def api():
    with patch("src.consumers.engineering_result_handler.api_client") as api:
        api.patch = AsyncMock()
        api.post = AsyncMock()
        api.get_run = AsyncMock(return_value=SimpleNamespace(run_metadata={}))
        api.get_primary_repository = AsyncMock(
            return_value=make_repository(git_url="https://github.com/org/product.git")
        )
        yield api


def _repository(monkeypatch, files: dict[str, str] | Exception) -> _GitHub | None:
    """Read the contract through the deploy's real loader, from this repository."""
    monkeypatch.setattr(
        engineering_result_handler,
        "_fetch_env_contract",
        env_contract_loader._fetch_env_contract,
    )
    if isinstance(files, Exception):
        monkeypatch.setattr(env_contract_loader, "GitHubAppClient", MagicMock(side_effect=files))
        return None
    github = _GitHub(files)
    monkeypatch.setattr(env_contract_loader, "GitHubAppClient", lambda: github)
    return github


async def _succeed(redis, *, planning_task_id: str | None = None) -> dict:
    return await engineering_result_handler.handle_engineering_success(
        EngineeringSuccessParams(
            result={
                "engineering_status": EngineeringStatus.DONE,
                "commit_sha": COMMIT,
                "worker_id": "w-1",
            },
            task_id=TASK_ID,
            project=make_project(name="product", config={"modules": ["backend"]}),
            callback_stream="po:response:abc",
            redis=redis,
            skip_deploy=False,
            developer_started_at=datetime.now(UTC),
            telegram_chat_id="u-1",
            action="feature",
            planning_task_id=planning_task_id,
            story_id="story-92b433c8",
        )
    )


def _deploy_publishes(redis) -> list:
    return [c for c in redis.publish_message.await_args_list if c.args[0] == DEPLOY_QUEUE]


def _deploy_runs(api) -> list:
    return [c for c in api.post.await_args_list if c.args[0] == "runs/"]


def _terminal_run_patch(api) -> dict:
    [*_, last] = [c for c in api.patch.await_args_list if c.args[0] == f"runs/{TASK_ID}"]
    return last.kwargs["json"]


def _task_transitions(api) -> list[str]:
    return [
        c.kwargs["params"]["to_status"]
        for c in api.post.await_args_list
        if c.args[0] == f"tasks/{PLANNING_TASK_ID}/transition"
    ]


@pytest.mark.asyncio
@patch("src.consumers.engineering_result_handler.set_story_worker", new_callable=AsyncMock)
class TestStory92b433c8:
    async def test_a_required_public_base_url_fails_engineering_and_creates_no_deploy(
        self, _set_worker, monkeypatch, api, mock_redis
    ):
        github = _repository(
            monkeypatch, _with_backend_entry("PUBLIC_BASE_URL", _derived(required=True))
        )

        out = await _succeed(mock_redis)

        assert out["status"] == "failed" and out["error"] == REASON
        assert github.refs == {COMMIT}
        assert _deploy_runs(api) == [] and _deploy_publishes(mock_redis) == []
        terminal = _terminal_run_patch(api)
        assert terminal["status"] == RunStatus.FAILED.value
        assert terminal["error_message"] == REASON
        result = EngineeringRunResult.model_validate(terminal["result"])
        assert result.failure_reason is EngineeringFailureReason.UNCOMPUTABLE_DERIVED_KEY
        assert result.uncomputable_derived_keys == ["PUBLIC_BASE_URL"]
        failed = [
            c for c in mock_redis.publish_flat.await_args_list if c.args[1]["event"] == "failed"
        ]
        assert len(failed) == 1

    async def test_a_task_attempt_is_failed_for_the_supervisor_to_retry(
        self, _set_worker, monkeypatch, api, mock_redis
    ):
        _repository(monkeypatch, _with_backend_entry("PUBLIC_BASE_URL", _derived(required=True)))

        await _succeed(mock_redis, planning_task_id=PLANNING_TASK_ID)

        assert _task_transitions(api) == ["failed"]
        assert _deploy_runs(api) == [] and _deploy_publishes(mock_redis) == []

    async def test_every_uncomputable_key_is_named(self, _set_worker, monkeypatch, api, mock_redis):
        files = _with_backend_entry("PUBLIC_BASE_URL", _derived(required=True))
        backend = yaml.safe_load(files["services/backend/env.contract.yaml"])
        backend["entries"]["WEBHOOK_URL"] = _derived(required=True)
        files["services/backend/env.contract.yaml"] = yaml.safe_dump(backend)
        _repository(monkeypatch, files)

        await _succeed(mock_redis)

        result = EngineeringRunResult.model_validate(_terminal_run_patch(api)["result"])
        assert result.uncomputable_derived_keys == ["PUBLIC_BASE_URL", "WEBHOOK_URL"]
        message = _terminal_run_patch(api)["error_message"]
        assert "cannot compute PUBLIC_BASE_URL;" in message
        assert "cannot compute WEBHOOK_URL;" in message


@pytest.mark.asyncio
@patch("src.consumers.engineering_result_handler.set_story_worker", new_callable=AsyncMock)
class TestContractsThatStillDeploy:
    async def test_the_kit_contract_deploys_exactly_as_before(
        self, _set_worker, monkeypatch, api, mock_redis
    ):
        """Only computable derived keys, plus the kit's optional PORT."""
        github = _repository(monkeypatch, _kit_fragments())

        out = await _succeed(mock_redis)

        assert out["status"] == "success"
        assert github.refs == {COMMIT}
        assert len(_deploy_runs(api)) == 1
        [published] = _deploy_publishes(mock_redis)
        assert published.args[1].head_sha == COMMIT
        assert _terminal_run_patch(api)["status"] == RunStatus.COMPLETED.value

    @pytest.mark.parametrize(
        "entry",
        [
            _derived(required=False),
            _derived(required=True, environments=("local",)),
        ],
        ids=["optional", "not-in-production"],
    )
    async def test_an_uncomputable_key_the_deploy_skips_is_allowed(
        self, _set_worker, monkeypatch, api, mock_redis, entry
    ):
        _repository(monkeypatch, _with_backend_entry("PUBLIC_BASE_URL", entry))

        out = await _succeed(mock_redis)

        assert out["status"] == "success"
        assert len(_deploy_publishes(mock_redis)) == 1

    @pytest.mark.parametrize(
        "files",
        [
            {"services/backend/env.contract.yaml": "entries: [not, a, mapping"},
            {"services/backend/env.contract.yaml": "version: '1'\nowner: backend\nentries: {}\n"}
            | {"services/tg_bot/env.contract.yaml": "version: '9'\nentries: {}\n"},
            {},
            RuntimeError("GitHub is unreachable"),
        ],
        ids=["unparseable", "invalid", "missing", "unreadable"],
    )
    async def test_a_contract_that_cannot_be_read_keeps_todays_path(
        self, _set_worker, monkeypatch, api, mock_redis, files
    ):
        """The deploy reports an unreadable contract; this check adds no failure."""
        _repository(monkeypatch, files)

        out = await _succeed(mock_redis)

        assert out["status"] == "success"
        assert len(_deploy_publishes(mock_redis)) == 1
