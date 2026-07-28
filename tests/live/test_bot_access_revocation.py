"""Revocation proved against the deployed bot, not against a config value.

Every other check in this area reads what a deploy would ship. This one asks the
bot: it drives a real grant on a really deployed project, watches the QA identity
get in, kills the run in the middle, and then requires the same bot to refuse
that identity. The request and the reading of the answer are the QA runner's own
(`shared.telegram_access_probe`), so a bot that ignored the test slot, or failed
open, fails here.

Needs a project that is already deployed with a private bot whose commit declares
the test identity slot, named by ``BOT_ACCESS_PROJECT_ID``. The stack must be up
(`make up`), because the scheduler sweep is what deploys the grant and takes it
back — the test never clears the value itself, otherwise it would be proving its
own cleanup rather than the pipeline's.

    BOT_ACCESS_PROJECT_ID=<uuid> uv run pytest tests/live/test_bot_access_revocation.py

Excluded from the offline live regressions: it deploys twice and talks to
Telegram.
"""

from __future__ import annotations

import asyncio
import os
import time
import uuid

import asyncssh
import httpx
from pipeline_helpers import API_URL, internal_headers
import pytest

from shared.contracts.bot_access import QA_TEST_TELEGRAM_ID, TEST_IDENTITY_ENV_KEY
from shared.contracts.dto.run import RunStatus, RunType
from shared.contracts.dto.run_result import QABlockerCategory
from shared.contracts.dto.temporary_access import (
    REVOKE_CONFIRMATION_WINDOW,
    TemporaryAccessStatus,
)
from shared.telegram_access_probe import build_access_probe_command, classify_access_probe

# The QA runner reads its Telethon credentials from the QA user's home; the probe
# is the same command, so it reads the same file.
TELETHON_ENV_FILE = "$HOME/.qa-telethon.env"

GRANT_TIMEOUT = 900  # a real deploy of the pinned commit, plus the sweep's cycle
# The same, plus the confirmation window: the record is closed by readings of the
# running service that agree over that span, not by the deploy that asked.
REVOKE_TIMEOUT = 900 + int(REVOKE_CONFIRMATION_WINDOW.total_seconds())
POLL_INTERVAL = 5

PROJECT_ID = os.getenv("BOT_ACCESS_PROJECT_ID", "")

pytestmark = pytest.mark.skipif(
    not PROJECT_ID,
    reason="set BOT_ACCESS_PROJECT_ID to a deployed project with a private bot",
)


class DeployedBot:
    """Where the bot runs and how to reach it as the QA account."""

    def __init__(
        self,
        *,
        bot_username: str,
        head_sha: str,
        application: dict,
        deployed_url: str,
        server: dict,
        ssh_key: str,
    ):
        self.bot_username = bot_username
        self.head_sha = head_sha
        self.application = application
        self.deployed_url = deployed_url
        self.server = server
        self.ssh_key = ssh_key

    async def admits_qa_identity(self) -> bool:
        """Send /start as the QA account and read what the bot answers."""
        async with asyncssh.connect(
            self.server["public_ip"],
            username=self.server["ssh_user"],
            client_keys=[asyncssh.import_private_key(self.ssh_key)],
            known_hosts=None,
        ) as conn:
            result = await conn.run(
                build_access_probe_command(self.bot_username, TELETHON_ENV_FILE), check=False
            )
        blocker = classify_access_probe(
            exit_status=result.exit_status,
            stdout=result.stdout or "",
            stderr=result.stderr or "",
            bot_username=self.bot_username,
        )
        if blocker is None:
            return True
        assert blocker.category is QABlockerCategory.TELEGRAM_ACCESS_DENIED, (
            f"the probe could not decide whether the bot admits QA: {blocker.received}"
        )
        return False


async def _resolve_deployed_bot(api: httpx.AsyncClient) -> DeployedBot:
    repos = (await api.get("/api/repositories/", params={"project_id": PROJECT_ID})).json()
    repo = next((r for r in repos if r.get("bot_username")), None)
    assert repo, f"project {PROJECT_ID} has no bot username on its repository"
    bot_username = repo["bot_username"]

    applications = (
        await api.get("/api/applications/", params={"repo_id": repo["id"], "status": "running"})
    ).json()
    assert applications, f"project {PROJECT_ID} has no running application to test against"
    application = applications[0]

    deployments = (
        await api.get(
            "/api/service-deployments/",
            params={"application_id": application["id"], "result": "success"},
        )
    ).json()
    assert deployments, f"application {application['id']} has no successful deployment"
    head_sha = deployments[0]["deployed_sha"]
    assert head_sha, "the deployment records no commit, so a grant cannot pin one"

    handle = application["server_handle"]
    server = (await api.get(f"/api/servers/{handle}")).json()
    ssh_key = (await api.get(f"/api/servers/{handle}/ssh-key")).json()["ssh_key"]
    ports = application.get("ports") or [{}]
    return DeployedBot(
        bot_username=bot_username,
        head_sha=head_sha,
        application=application,
        deployed_url=f"http://{server['public_ip']}:{ports[0].get('port', 80)}",
        server=server,
        ssh_key=ssh_key,
    )


async def _create_qa_run(api: httpx.AsyncClient) -> str:
    run_id = f"qa-bot-access-{uuid.uuid4().hex[:8]}"
    resp = await api.post(
        "/api/runs/",
        json={
            "id": run_id,
            "type": RunType.QA.value,
            "project_id": PROJECT_ID,
            "run_metadata": {"triggered_by": "bot_access_revocation_live_test"},
        },
    )
    resp.raise_for_status()
    return run_id


async def _create_grant(api: httpx.AsyncClient, bot: DeployedBot, qa_run_id: str) -> str:
    grant_id = f"tempaccess-live-{uuid.uuid4().hex[:8]}"
    resp = await api.post(
        "/api/temporary-access-grants/",
        json={
            "id": grant_id,
            "project_id": PROJECT_ID,
            "env_key": TEST_IDENTITY_ENV_KEY,
            "subject": str(QA_TEST_TELEGRAM_ID),
            "head_sha": bot.head_sha,
            "qa_run_id": qa_run_id,
            "grant_run_id": f"deploy-grant-{uuid.uuid4().hex[:8]}",
            "qa_message": {
                "story_id": "",
                "project_id": PROJECT_ID,
                "user_id": "",
                "deployed_url": bot.deployed_url,
                "application_id": bot.application["id"],
                "acceptance_criteria": "live bot access revocation check",
                "bot_username": bot.bot_username,
                "run_id": qa_run_id,
            },
        },
    )
    resp.raise_for_status()
    return grant_id


async def _wait_for_grant_status(
    api: httpx.AsyncClient, grant_id: str, wanted: set[str], timeout: float
) -> dict:
    deadline = time.monotonic() + timeout
    grant = {}
    while time.monotonic() < deadline:
        grant = (await api.get(f"/api/temporary-access-grants/{grant_id}")).json()
        if grant["status"] in wanted:
            return grant
        await asyncio.sleep(POLL_INTERVAL)
    raise AssertionError(
        f"grant {grant_id} stayed {grant.get('status')} for {timeout}s, wanted one of {wanted}"
    )


@pytest.mark.asyncio
async def test_a_killed_qa_run_leaves_the_bot_refusing_the_test_identity():
    """The card's case: the run dies mid-flight and the access still goes back.

    Nothing here revokes anything. The QA run is killed and the scheduler sweep
    is left to notice, which is the whole claim: revocation follows from the
    state, so it happens without the process that granted the access.
    """
    async with httpx.AsyncClient(base_url=API_URL, timeout=30, headers=internal_headers()) as api:
        bot = await _resolve_deployed_bot(api)

        assert not await bot.admits_qa_identity(), (
            f"@{bot.bot_username} already admits the QA identity, so this run could not "
            "tell a revocation from the starting state"
        )

        qa_run_id = await _create_qa_run(api)
        grant_id = await _create_grant(api, bot, qa_run_id)

        granted = await _wait_for_grant_status(
            api,
            grant_id,
            {TemporaryAccessStatus.GRANTED.value},
            GRANT_TIMEOUT,
        )
        assert granted["qa_dispatched_at"], "the sweep confirmed the access without releasing QA"
        assert await bot.admits_qa_identity(), (
            f"@{bot.bot_username} still refuses the QA identity after the grant deploy "
            "reported success"
        )

        # Kill the run in the middle. From here on nobody is left who knows the
        # access was handed out except the grant record.
        killed = await api.patch(
            f"/api/runs/{qa_run_id}", json={"status": RunStatus.CANCELLED.value}
        )
        killed.raise_for_status()

        revoked = await _wait_for_grant_status(
            api, grant_id, {TemporaryAccessStatus.REVOKED.value}, REVOKE_TIMEOUT
        )
        assert revoked["revoke_reason"] == "run_terminal"

        assert not await bot.admits_qa_identity(), (
            f"@{bot.bot_username} still admits the QA identity after the grant was revoked"
        )
