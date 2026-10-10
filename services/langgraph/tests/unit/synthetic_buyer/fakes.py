"""An in-process world for the synthetic buyer: Telegram, the Codegen API, the product.

The Codegen bot here answers the way the released PO's scenario does (token, then
a project bound to that token, then the feature, a preview and a language question,
the brief it points the project at, the accepted order) and records into the fake
API, the project's secrets and the repository exactly what the real ones would
expose. Judgment stays in the controller and its adapters; the world only holds
facts. Time is a fake clock that moves only when the controller sleeps; native work,
QA runs and channel posts happen on those sleeps. Native QA's Telegram use takes
the real `TelegramIdentityLease` over an in-memory Redis with Lua, the same one the
buyer holds. Nothing starts a process, opens a socket or really sleeps.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
import inspect
from typing import Any

import fakeredis
from fakeredis.aioredis import FakeRedis

from shared.catalog_activation import CATALOG_ACTIVATION
from shared.clients.registry import sha_image_tag
from shared.contracts.dto.capability_preview import CapabilityPlan
from src.consumers._qa_telegram_lease import LEASE_KEY, Holder, HolderKind, TelegramIdentityLease
from src.synthetic_buyer.codegen_api import ApiRefused
from src.synthetic_buyer.config import parse_config
from src.synthetic_buyer.controller import Clock, SyntheticBuyer
from src.synthetic_buyer.evidence import EvidenceStore, Redaction, new_record
from src.synthetic_buyer.persona import PersonaContext, PersonaDecision, PersonaTurn
from src.synthetic_buyer.platform_evidence import AuthFacts, UsageFacts, platform_product_id
from src.synthetic_buyer.repository_evidence import (
    CompareFacts,
    JobFacts,
    PullRequestFacts,
    RepositoryFactUnavailable,
    WorkflowRunFacts,
)
from src.synthetic_buyer.telegram import Button, Message, Peer, TransportError

BUYER = 8202532144
USER_ID = 21
CODEGEN = Peer(id=7001, username="codegen_orch_bot", is_bot=True)
PRODUCT = Peer(id=7002, username="channels_digest_bot", is_bot=True)
BOTFATHER = Peer(id=93372553, username="BotFather", is_bot=True)
STRANGER = 5550001
TOKEN = "7712345678:" + "AAH" + "k" * 32
OTHER_TOKEN = "7799999999:" + "BBH" + "z" * 32
PROMO = "PROMOCODEVALUE" + "Q" * 10
SESSION = "1BVtsOK" + "s" * 60
API_HASH = "f" * 32
INTERNAL_KEY = "internal-key-value-0123456789"
UNRELATED_PROJECT = "11111111-1111-4111-8111-111111111111"
PROJECT = "22222222-2222-4222-8222-222222222222"
SIDE_PROJECT = "33333333-3333-4333-8333-333333333333"
STORY = "story-0001"
BRIEF = "brief-" + "a" * 24
PREVIEW = "preview-" + "1" * 24
REPOSITORY = "product-org/channels-digest"
CHANNELS = ["chan_one", "chan_two"]
START = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)
REVISION = "f090f9ad6682c28de4e508a24082f8b2cbcc7770"
SCAFFOLD = "0" * 39 + "1"
INSTALL_HEAD = "0" * 39 + "2"
PR_HEAD = "0" * 39 + "3"
MERGE = "0" * 39 + "4"
#: The commit's own `ci.yml` run on main (the scheduler's publication observation).
PUBLICATION_RUN = 9001
#: The `deploy.yml` run the deployer dispatched and stored as `deployment_result.run_id`.
DEPLOY_WORKFLOW_RUN = 9100
#: The PR's own CI run: green, but on the branch head, not the merged commit.
PR_CI_RUN = 8
DIGEST = "sha256:" + "c" * 64
IMAGE = f"registry.example.test/{REPOSITORY}-backend:{sha_image_tag(MERGE)}"


def config_data(**overrides: Any) -> dict:
    data = {
        "schema_version": 2,
        "operation_id": "s1487-buyer-001",
        "codegen_bot": {"username": CODEGEN.username, "user_id": CODEGEN.id},
        "buyer": {"telegram_id": BUYER},
        "scenario": {
            "public_channels": [f"@{name}" for name in CHANNELS],
            "product_language": "ru",
            "switch_language": "en",
        },
        "model": {"chain": [{"channel": "openrouter", "model": "persona-model"}]},
        "deadlines": {
            "reply_seconds": 60,
            "settle_seconds": 4,
            "conversation_turns": 8,
            "order_seconds": 3600,
            "build_seconds": 7200,
            "identity_wait_seconds": 600,
            "probe_reply_seconds": 30,
            "post_delivery_seconds": 600,
            "teardown_seconds": 600,
            "poll_seconds": 30,
            "delivery_checks": 3,
            "deferrals": 3,
        },
        "evidence_dir": "/evidence",
        "api": {"base_url": "http://api:8000"},
        "telegram": {
            "api_id": {"env": "TELETHON_API_ID"},
            "api_hash": {"env": "TELETHON_API_HASH"},
            "session": {"env": "TELETHON_SESSION"},
        },
        "identity_lease": {"redis_url": {"env": "REDIS_URL"}},
        "registration": {"credits_microusd": 5_000_000, "attempt_reservation_microusd": 500_000},
        "product_token": {"mode": "handle", "handle": {"env": "BUYER_PRODUCT_BOT_TOKEN"}},
        "platform": {
            "auth_admin_url": {"env": "PLATFORM_AUTH_ADMIN_URL"},
            "auth_admin_token": {"env": "PLATFORM_AUTH_ADMIN_TOKEN"},
            "reader_base_url": "https://reader.example.test/channels",
        },
    }
    for key, value in overrides.items():
        data[key] = value
    return data


ENVIRON = {
    "INTERNAL_API_KEY": INTERNAL_KEY,
    "TELETHON_API_ID": "12345",
    "TELETHON_API_HASH": API_HASH,
    "TELETHON_SESSION": SESSION,
    "REDIS_URL": "redis://:redis-password-value@redis:6379/0",
    "BUYER_PRODUCT_BOT_TOKEN": TOKEN,
    "PLATFORM_AUTH_ADMIN_URL": "http://auth:8000",
    "PLATFORM_AUTH_ADMIN_TOKEN": "admin-token-value-abcdef",
    "SECRETS_ENCRYPTION_KEY": "runtime-key",
    "GITHUB_APP_ID": "12345",
    "GITHUB_APP_PRIVATE_KEY_PATH": "/app/keys/github_app.pem",
}


def install() -> dict:
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


def install_operation(**changes: Any) -> dict:
    payload = install()
    operation = {
        "id": "op-1",
        "project_id": PROJECT,
        "task_id": "t-install",
        "story_id": STORY,
        "repository_id": "repo-1",
        "cycle_started_at": "2026-10-10T12:10:00+00:00",
        "state": "published",
        "stage": "published",
        "token": "install-op-secret-token",
        "base_sha": SCAFFOLD,
        "head_sha": INSTALL_HEAD,
        "verification": {
            "core_version": payload["core_version"],
            "tooling_commit": payload["tooling_commit"],
            "binding_sha256": "b" * 64,
            "distributions": {"tg-channels": "0.1.2"},
            "component_targets": {},
            "protected_sha256": {},
        },
        "preflight": {
            "result_version": 1,
            "package": "tg-channels",
            "status": "mechanical",
            "product_core": payload["core_version"],
            "target": {
                "route": "catalog",
                "catalog_source": payload["catalog"]["repository"],
                "catalog_ref": payload["catalog"]["commit"],
                "tag": payload["package"]["tag"],
                "version": "0.1.2",
                "requires_core": ">=2.4,<3",
                "metadata_sha256": "d" * 64,
            },
            "glue": [],
            "incompatible": None,
        },
    }
    operation.update(changes)
    return operation


def plan(route: str = "module") -> CapabilityPlan:
    capability = {"request_id": "channels", "route": route, "requirement_ids": ["req-1"]}
    if route != "from_scratch":
        capability |= {"capability_id": "cap-5de1cd8d9a7c", "install": install()}
    return CapabilityPlan.model_validate(
        {
            "preview_id": PREVIEW,
            "activation": CATALOG_ACTIVATION.model_dump(),
            "capabilities": [capability],
        }
    )


class ProcessDied(BaseException):  # noqa: N818 - the process, not an error, ended
    """The operator's process died mid-operation: nothing in it may catch this."""


@dataclass
class FakeClock:
    now: datetime = START
    hooks: list[Callable[[], Any]] = field(default_factory=list)

    def wall(self) -> datetime:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)
        for hook in list(self.hooks):
            result = hook()
            if inspect.isawaitable(result):
                await result

    def clock(self) -> Clock:
        return Clock(wall=self.wall, sleep=self.sleep)


async def _never() -> None:
    """A renewal that never comes due inside a test: the watchdog sleeps until cancelled."""
    await asyncio.Event().wait()


def initialized_redis() -> FakeRedis:
    """A Redis whose QA identity record an operator initialized to idle."""
    server = fakeredis.FakeServer()
    fakeredis.FakeStrictRedis(server=server).hset(
        LEASE_KEY.format(telegram_id=BUYER), mapping={"state": "idle"}
    )
    return FakeRedis(server=server)


def identity_lease(redis: FakeRedis, clock: FakeClock) -> TelegramIdentityLease:
    """The real lease over the world's Redis, timed by the fake clock."""
    return TelegramIdentityLease(
        redis,
        BUYER,
        wall=lambda: clock.now.timestamp(),
        sleep=clock.sleep,
        renew_wait=_never,
        now=clock.wall,
    )


class NativeQA:
    """Native QA's side of the shared identity: holds it exactly as the QA consumer does."""

    def __init__(self, world: World) -> None:
        self.world = world
        self.token: str | None = None
        self.denied = 0
        self.spans: list[tuple[datetime, datetime | None]] = []

    @property
    def holding(self) -> bool:
        return self.token is not None

    async def start(self, reference: str = "qa-other", purpose: str = "exploratory") -> bool:
        """Take the identity if it is free right now; a denial is counted, not waited."""
        if self.token is not None:
            return True
        lease = identity_lease(self.world.redis, self.world.clock)
        try:
            self.token = await lease._acquire(  # noqa: SLF001 - the consumer's own admission
                Holder(HolderKind.NATIVE_QA, reference, purpose), 0, 0
            )
        except Exception:  # noqa: BLE001 - IdentityBusy: the buyer holds it
            self.denied += 1
            return False
        self.spans.append((self.world.clock.now, None))
        return True

    async def end(self) -> None:
        if self.token is None:
            return
        await identity_lease(self.world.redis, self.world.clock).release(self.token)
        self.token = None
        started, _ = self.spans[-1]
        self.spans[-1] = (started, self.world.clock.now)


class World:
    """Both sides of every boundary the controller crosses, in one consistent state."""

    def __init__(self, clock: FakeClock) -> None:  # noqa: PLR0915 - one world's facts
        self.clock = clock
        self.dialogs: dict[int, list[Message]] = {CODEGEN.id: [], PRODUCT.id: [], BOTFATHER.id: []}
        self.next_id = 500
        self.users: dict[int, dict] = {}
        self.promos: list[dict] = []
        #: The promo API answers without this operation's retained codes.
        self.promos_withheld = False
        self.projects: dict[str, dict] = {
            UNRELATED_PROJECT: {
                "id": UNRELATED_PROJECT,
                "owner_id": 99,
                "created_at": "2026-01-01T00:00:00+00:00",
                "initiating_run_id": "po-unrelated",
                "config": {},
            }
        }
        self.secrets: dict[str, dict] = {}
        self.rollout: dict = {"project_ids": [UNRELATED_PROJECT], "note": "operator-owned"}
        self.rollout_writes: list[dict] = []
        self.previews: dict[str, dict] = {}
        self.briefs: dict[str, dict] = {}
        self.stories: list[dict] = []
        self.plans: dict[str, CapabilityPlan] = {}
        self.tasks: list[dict] = []
        self.runs: list[dict] = []
        self.redis = initialized_redis()
        self.connected_at: list[datetime] = []
        self.native = NativeQA(self)
        self.language = "ru"
        self.teardown = "completed"
        self.sends_while_qa_busy: list[str] = []
        self.connects_while_qa_busy = 0
        self.reads_while_qa_busy = 0
        self.stage = "new"
        self.route = "module"
        #: The product delivers one post of a configured channel on its own,
        #: in the `tg-channels.post` event's form, a while after `/channels`.
        self.post_event = True
        self.build_ticks = 2
        self.qa_phase = "waiting"
        self.story_final = "completed"
        self.auth = AuthFacts("orch-x", "abcdefghijk2", ("abcdefghijk2",), ())
        self.usage = UsageFacts(platform_product_id(PROJECT), 2, 1, 2)
        self.language_switch_works = True
        self.deploy_project = PROJECT
        self.deploy_result: dict = {
            "deploy_outcome": "success",
            "deployed_url": "https://product.example.test",
            "application_id": 5,
            "bot_username": PRODUCT.username,
            # The deployer's own shape: `run_id` is the deploy.yml run it dispatched.
            "deployment_result": {
                "status": "success",
                "run_id": DEPLOY_WORKFLOW_RUN,
                "deployed_commit_sha": MERGE,
                "image_references": {"BACKEND_IMAGE": IMAGE},
                "image_digests": {"BACKEND_IMAGE": DIGEST},
            },
        }
        self.operation = install_operation()
        #: The run the scheduler recorded as the merged commit's publication.
        self.timeline_publication_id = PUBLICATION_RUN
        self.engineering_files: tuple[str, ...] = ()
        #: GitHub's Actions runs of the product repository, by id.
        self.workflow_runs: dict[int, WorkflowRunFacts] = {
            PR_CI_RUN: WorkflowRunFacts(
                PR_CI_RUN,
                PR_HEAD,
                "completed",
                "success",
                ".github/workflows/ci.yml",
                "story-0001",
                "pull_request",
            ),
            PUBLICATION_RUN: WorkflowRunFacts(
                PUBLICATION_RUN,
                MERGE,
                "completed",
                "success",
                ".github/workflows/ci.yml",
                "main",
                "push",
            ),
            DEPLOY_WORKFLOW_RUN: WorkflowRunFacts(
                DEPLOY_WORKFLOW_RUN,
                MERGE,
                "completed",
                "success",
                ".github/workflows/deploy.yml",
                f"deploy-{MERGE[:12]}",
                "workflow_dispatch",
            ),
        }
        self.jobs: dict[int, list[JobFacts]] = {
            PUBLICATION_RUN: [
                JobFacts(
                    "build-and-push (backend, ., services/backend/Dockerfile, backend)",
                    "completed",
                    "success",
                ),
            ],
            PR_CI_RUN: [JobFacts("lint-and-test", "completed", "success")],
        }
        self.channel_posts: dict[tuple[str, int], datetime] = {}
        self.botfather_state: str | None = None
        self.botfather_bots: list[str] = []
        self.botfather_dies_after_creation = False
        self.events: list[tuple] = []

    # --- Telegram side ---------------------------------------------------------

    def append(  # noqa: PLR0913 - every field a message may carry
        self,
        peer: Peer,
        text: str,
        *,
        sender: int | None = None,
        out: bool = False,
        urls: tuple[str, ...] = (),
        buttons: tuple[Button, ...] = (),
        reply_to: int | None = None,
    ) -> Message:
        self.next_id += 1
        message = Message(
            id=self.next_id,
            sender_id=BUYER if out else (sender if sender is not None else peer.id),
            outgoing=out,
            date=self.clock.now,
            text=text,
            urls=urls,
            buttons=buttons,
            reply_to=reply_to,
        )
        self.dialogs[peer.id].append(message)
        return message

    def create_project(self, project_id: str, token: str | None, *, run: str) -> None:
        self.projects[project_id] = {
            "id": project_id,
            "owner_id": USER_ID,
            "created_at": self.clock.now.isoformat(),
            "status": "draft",
            "initiating_run_id": run,
            "config": {},
        }
        self.secrets[project_id] = {} if token is None else {"TELEGRAM_BOT_TOKEN": token}

    def codegen_answer(self, text: str) -> list[str]:  # noqa: PLR0911 - the PO scenario
        if text == PROMO:
            self.users[BUYER] = {"id": USER_ID, "telegram_id": BUYER}
            self.promos[-1]["redeemed_by_user_id"] = USER_ID
            return ["Промокод активирован. Добро пожаловать!"]
        if BUYER not in self.users:
            return ["Чтобы начать, пришлите одноразовый промокод."]
        if text == TOKEN:
            self.create_project(PROJECT, TOKEN, run="po-0123456789ab")
            self.stage = "project"
            return [f"Проект создан, бот подключён (echo {TOKEN}). Что должен делать бот?"]
        if self.stage == "new":
            return ["Отлично! Пришлите, пожалуйста, токен бота от @BotFather."]
        if self.stage == "project":
            self.previews[PREVIEW] = {
                "preview_id": PREVIEW,
                "project_id": PROJECT,
                "created_at": self.clock.now.isoformat(),
            }
            self.stage = "preview"
            words = {"module": "готовым решением", "from_scratch": "с нуля"}[self.route]
            return [f"Это можно сделать {words}.", "Какой язык бота: русский или английский?"]
        if self.stage == "preview":
            self.stage = "brief"
            self.briefs[BRIEF] = {
                "id": BRIEF,
                "project_id": PROJECT,
                "revision": 1,
                "confirmed_at": None,
                "content": {"language": "ru"},
            }
            self.plans[BRIEF] = plan(self.route)
            self.projects[PROJECT]["config"]["product_brief_id"] = BRIEF
            return ["Описание заказа: бот присылает посты из каналов.\n\nда / поправить"]
        if self.stage == "brief":
            self.stage = "ordered"
            self.briefs[BRIEF]["confirmed_at"] = self.clock.now.isoformat()
            self.stories.append(
                {
                    "id": STORY,
                    "project_id": PROJECT,
                    "status": "in_progress",
                    "pr_number": 1,
                    "created_at": self.clock.now.isoformat(),
                }
            )
            self.clock.hooks.append(self.build_tick)
            return ["Заказ принят, приступаем к работе."]
        return ["Работа идёт."]

    def product_answer(self, text: str) -> list[tuple[str, tuple[str, ...]]]:
        ru = self.language == "ru"
        if text == "/start":
            return [
                (
                    "Привет! Команды: /channel, /channels, /digest"
                    if ru
                    else "Hello! Commands: /channel, /channels, /digest",
                    (),
                )
            ]
        if text == "/channels":
            if self.post_event and not hasattr(self, "channels_at"):
                self.channels_at = self.clock.now
                self.clock.hooks.append(self.deliver_post)
            if ru:
                return [(f"@{name}", ()) for name in CHANNELS]
            return [(f"Channel @{name}", ()) for name in CHANNELS]
        if text == "/digest":
            # The released `/digest` item: the post from the channel on, no prefix.
            url = "https://t.me/chan_one/100"
            self.channel_posts[("chan_one", 100)] = self.clock.now - timedelta(hours=5)
            return [(f"@chan_one\n2026-10-09\nСтарый пост\n{url}", (url,))]
        return []

    def deliver_post(self) -> None:
        """The product's own `tg-channels.post` event: its released form, unquoted."""
        if self.clock.now < self.channels_at + timedelta(seconds=120):
            return
        self.clock.hooks.remove(self.deliver_post)
        url = "https://t.me/chan_two/201"
        self.channel_posts[("chan_two", 201)] = self.clock.now - timedelta(seconds=30)
        self.append(PRODUCT, post_event("chan_two", "Новый пост", url), urls=(url,))

    def botfather_answer(self, text: str) -> list[str]:
        if text == "/newbot":
            self.botfather_state = "name"
            return ["Alright, a new bot. How are we going to call it?"]
        if self.botfather_state == "name":
            self.botfather_state = "username"
            return ["Good. Now let's choose a username for your bot."]
        if self.botfather_state == "username":
            self.botfather_state = None
            self.botfather_bots.append(text)
            if self.botfather_dies_after_creation:
                self.botfather_dies_after_creation = False
                raise ProcessDied
            return [f"Done! Use this token to access the HTTP API:\n{TOKEN}"]
        if text == "/token":
            self.botfather_state = "choose"
            return ["Choose a bot to generate a new token."]
        if self.botfather_state == "choose":
            self.botfather_state = None
            if text.removeprefix("@") in self.botfather_bots:
                return [f"You can use this token to access HTTP API:\n{TOKEN}"]
            return ["Invalid bot selected."]
        return ["Unrecognized command."]

    # --- native work ---------------------------------------------------------------

    async def build_tick(self) -> None:
        """Native work, then the story's own QA holding the identity for one tick.

        QA waits, tick by tick, while anyone else holds the identity.
        """
        self.build_ticks -= 1
        if self.build_ticks > 0:
            return
        if self.qa_phase == "waiting":
            if await self.native.start(reference="qa-1"):
                self.qa_phase = "holding"
            return
        if self.qa_phase == "holding":
            await self.native.end()
            self.qa_phase = "done"
        self.clock.hooks.remove(self.build_tick)
        story = self.stories[0]
        story["status"] = self.story_final
        story["generated_product_timeline"] = {
            "pull_request": {
                "number": 1,
                "state": "closed",
                "merged_at": "2026-10-10T13:00:00+00:00",
                "head_sha": PR_HEAD,
                "merge_commit_sha": MERGE,
            },
            # The scheduler's observations: the PR's CI, then the merged commit's
            # publication on main (`pr_poller._updated_generated_product_timeline`).
            "ci_runs": [
                {
                    "id": PR_CI_RUN,
                    "url": f"https://ci/{PR_CI_RUN}",
                    "conclusion": "success",
                    "head_sha": PR_HEAD,
                    "branch": "story-0001",
                },
                {
                    "id": self.timeline_publication_id,
                    "url": f"https://ci/{self.timeline_publication_id}",
                    "status": "completed",
                    "conclusion": "success",
                    "head_sha": MERGE,
                    "branch": "main",
                },
            ],
        }
        self.tasks[:] = [
            {
                "id": "t-install",
                "type": "install",
                "status": "done",
                "blocked_by_task_id": None,
                "created_at": "2026-10-10T12:10:00+00:00",
                "install": install(),
                "install_operation": self.operation,
            },
            {
                "id": "t-glue",
                "type": "feature",
                "status": "done",
                "blocked_by_task_id": "t-install",
                "created_at": "2026-10-10T12:11:00+00:00",
            },
        ]
        deployed = (self.clock.now - timedelta(minutes=5)).isoformat()
        self.runs[:] = [
            {
                "id": "eng-1",
                "type": "engineering",
                "status": "completed",
                "project_id": PROJECT,
                "story_id": STORY,
                "task_id": "t-glue",
                "completed_at": deployed,
                "result": None,
            },
            {
                "id": "dep-1",
                "type": "deploy",
                "status": "completed",
                "project_id": self.deploy_project,
                "story_id": STORY,
                "completed_at": deployed,
                "result": self.deploy_result,
            },
            {
                "id": "qa-1",
                "type": "qa",
                "status": "completed",
                "project_id": PROJECT,
                "story_id": STORY,
                "completed_at": self.clock.now.isoformat(),
                "result": {
                    "qa_outcome": "passed",
                    "deployed_url": "https://product.example.test",
                    "passed_checks": ["bot replies"],
                },
            },
        ]


class FakeTelegram:
    def __init__(self, world: World, *, me: int = BUYER) -> None:
        self.world = world
        self.me_id = me
        self._connected = False
        self.events: list[tuple] = []
        self.fail_next_sends = 0
        self.duplicate_reads = False
        #: Sent texts whose receipt is lost and whose outgoing message reads do not
        #: show until the test reveals them (Telegram's eventual consistency).
        self.unconfirmed: set[str] = set()
        self.hidden: set[str] = set()

    @property
    def connected(self) -> bool:
        return self._connected

    def _require(self) -> None:
        if not self._connected:
            raise TransportError("session", "not connected")

    async def connect(self) -> None:
        self.world.connected_at.append(self.world.clock.now)
        if self.world.native.holding:
            self.world.connects_while_qa_busy += 1
        self.events.append(("connect",))
        self.world.events.append(("connect",))
        self._connected = True

    async def disconnect(self) -> None:
        self.events.append(("disconnect",))
        self.world.events.append(("disconnect",))
        self._connected = False

    async def me(self) -> int:
        self._require()
        return self.me_id

    async def resolve(self, username: str) -> Peer:
        self._require()
        peers = {p.username.casefold(): p for p in (CODEGEN, PRODUCT, BOTFATHER)}
        return peers[username.casefold()]

    async def latest_id(self, peer: Peer) -> int:
        self._require()
        dialog = self.world.dialogs[peer.id]
        return dialog[-1].id if dialog else 0

    async def messages_after(self, peer: Peer, after_id: int) -> list[Message]:
        self._require()
        if self.world.native.holding:
            self.world.reads_while_qa_busy += 1
        found = [
            m
            for m in self.world.dialogs[peer.id]
            if m.id > after_id and not (m.outgoing and m.text in self.hidden)
        ]
        return found + found if self.duplicate_reads else found

    async def send(self, peer: Peer, text: str) -> Message:
        self._require()
        if self.world.native.holding:
            self.world.sends_while_qa_busy.append(text)
        self.events.append(("send", peer.username, text))
        self.world.events.append(("send", peer.username, text))
        sent = self.world.append(peer, text, out=True)
        if peer.id == CODEGEN.id:
            for reply in self.world.codegen_answer(text):
                self.world.append(peer, reply)
        elif peer.id == BOTFATHER.id:
            for reply in self.world.botfather_answer(text):
                self.world.append(peer, reply)
        elif peer.id == PRODUCT.id:
            for reply, urls in self.world.product_answer(text):
                self.world.append(peer, reply, urls=urls)
        if self.fail_next_sends:
            self.fail_next_sends -= 1
            raise TransportError("send", "failed: RPCError")
        if text in self.unconfirmed:
            self.hidden.add(text)
            raise TransportError("send", "did not answer in 30s")
        return sent

    async def press(self, peer: Peer, message_id: int, data: bytes) -> None:
        self._require()
        self.events.append(("press", peer.username, message_id, data))

    async def post_date(self, channel: str, post_id: int) -> datetime | None:
        self._require()
        return self.world.channel_posts.get((channel, post_id))


class FakeApi:
    def __init__(self, world: World) -> None:
        self.world = world
        self.calls: list[str] = []
        self.refuse: set[str] = set()

    def _call(self, name: str) -> None:
        self.calls.append(name)
        self.world.events.append(("api", name))
        if name in self.refuse:
            raise ApiRefused(f"/api/{name}", 503)

    async def user_by_telegram(self, telegram_id: int) -> dict | None:
        self._call("user_by_telegram")
        return self.world.users.get(telegram_id)

    async def mint_promo(self, *, credits_microusd: int, reservation_microusd: int) -> dict:
        self._call("mint_promo")
        promo = {
            "id": len(self.world.promos) + 1,
            "code": PROMO,
            "credits_microusd": credits_microusd,
            "attempt_reservation_microusd": reservation_microusd,
            "redeemed_by_user_id": None,
            "created_at": self.world.clock.now.isoformat(),
        }
        self.world.promos.append(promo)
        return dict(promo)

    async def promo_codes(self) -> list[dict]:
        self._call("promo_codes")
        if self.world.promos_withheld:
            return []
        return [dict(code) for code in self.world.promos]

    async def owned_projects(self, telegram_id: int) -> list[dict]:
        self._call("owned_projects")
        user = self.world.users[telegram_id]
        return [dict(p) for p in self.world.projects.values() if p["owner_id"] == user["id"]]

    async def project(self, project_id: str, *, as_user: int | None = None) -> dict:
        self._call("project")
        project = self.world.projects[project_id]
        return {**project, "config": dict(project["config"])}

    async def module_rollout(self) -> dict:
        self._call("module_rollout")
        return dict(self.world.rollout)

    async def write_module_rollout(self, value: dict) -> dict:
        self._call("write_module_rollout")
        self.world.rollout_writes.append({"value": value, "at": self.world.clock.now})
        self.world.rollout = dict(value)
        return dict(value)

    async def stories(self, project_id: str) -> list[dict]:
        self._call("stories")
        return [dict(s) for s in self.world.stories if s["project_id"] == project_id]

    async def story(self, story_id: str) -> dict:
        self._call("story")
        return dict(next(s for s in self.world.stories if s["id"] == story_id))

    async def brief_by_story(self, story_id: str) -> dict | None:
        self._call("brief_by_story")
        if story_id != STORY or BRIEF not in self.world.briefs:
            return None
        return dict(self.world.briefs[BRIEF])

    async def brief(self, brief_id: str) -> dict:
        self._call("brief")
        return dict(self.world.briefs[brief_id])

    async def repositories(self, project_id: str) -> list[dict]:
        self._call("repositories")
        return [{"id": "repo-1", "git_url": f"https://github.com/{REPOSITORY}"}]

    async def capability_plan(self, brief_id: str) -> CapabilityPlan | None:
        self._call("capability_plan")
        return self.world.plans.get(brief_id)

    async def capability_preview(self, preview_id: str) -> dict:
        self._call("capability_preview")
        return dict(self.world.previews[preview_id])

    async def tasks(self, story_id: str) -> list[dict]:
        self._call("tasks")
        return [dict(t) for t in self.world.tasks]

    async def runs(self, *, story_id=None, run_type=None, status=None) -> list[dict]:
        self._call("runs")
        return [
            r for r in self.world.runs if r["type"] == run_type.value and r["story_id"] == story_id
        ]

    async def bot_liveness(self, project_id: str) -> dict:
        self._call("bot_liveness")
        return {"state": "alive", "bot_username": PRODUCT.username}

    async def request_teardown(self, project_id: str, telegram_id: int) -> dict:
        self._call("request_teardown")
        return {"status": "pending", "project_status": "active", "pending_application_ids": [5]}

    async def teardown_state(self, project_id: str, telegram_id: int) -> dict:
        self._call("teardown_state")
        if self.world.teardown == "completed":
            self.world.projects[project_id]["status"] = "archived"
            return {
                "status": "completed",
                "project_status": "archived",
                "released_bot_username": PRODUCT.username,
            }
        return {"status": "failed", "project_status": "active", "error": "undeploy failed"}


class FakeRepository:
    """Read-only repository facts of the generated product."""

    def __init__(self, world: World) -> None:
        self.world = world
        self.unavailable: set[str] = set()

    def _check(self, fact: str) -> None:
        if fact in self.unavailable:
            raise RepositoryFactUnavailable(f"{fact}: HTTP 404")

    async def pull_request(self, repository: str, number: int) -> PullRequestFacts:
        self._check("pull_request")
        return PullRequestFacts(number, True, PR_HEAD, SCAFFOLD, MERGE)

    async def compare(self, repository: str, base: str, head: str) -> CompareFacts:
        self._check("compare")
        if (base, head) == (SCAFFOLD, INSTALL_HEAD):
            return CompareFacts(base, head, "ahead", ("install-commit",), ("pyproject.toml",))
        if (base, head) == (INSTALL_HEAD, PR_HEAD):
            commits = ("glue-commit",) if self.world.engineering_files else ()
            status = "ahead" if commits else "identical"
            return CompareFacts(base, head, status, commits, self.world.engineering_files)
        return CompareFacts(base, head, "diverged", (), ())

    async def workflow_run(self, repository: str, run_id: int) -> WorkflowRunFacts:
        self._check("workflow_run")
        if run_id not in self.world.workflow_runs:
            raise RepositoryFactUnavailable("actions run: HTTP 404")
        return self.world.workflow_runs[run_id]

    async def publication_runs(self, repository: str, commit: str) -> list[WorkflowRunFacts]:
        """GitHub's filter: `ci.yml` runs on main at the commit, whatever they concluded."""
        self._check("publication_runs")
        return [
            run
            for run in self.world.workflow_runs.values()
            if run.path == ".github/workflows/ci.yml"
            and run.head_branch == "main"
            and run.head_sha == commit
        ]

    async def workflow_jobs(self, repository: str, run_id: int) -> list[JobFacts]:
        self._check("workflow_jobs")
        return list(self.world.jobs.get(run_id, []))


class FakePlatform:
    def __init__(self, world: World) -> None:
        self.world = world

    async def auth(self, project_id: str) -> AuthFacts:
        return self.world.auth

    async def usage(self, project_id: str, reader_base_url: str) -> UsageFacts:
        return self.world.usage

    async def switch_language(self, project_id: str, deployed_url: str, language: str) -> bool:
        if self.world.language_switch_works:
            self.world.language = language
        return self.world.language_switch_works


@dataclass
class ScriptedPersona:
    """A deterministic customer: answers the scenario's bot messages, records what it saw."""

    contexts: list[PersonaContext] = field(default_factory=list)
    overrides: dict[str, PersonaTurn] = field(default_factory=dict)
    interrupt_on: str | None = None

    async def turn(self, context: PersonaContext) -> PersonaTurn:
        self.contexts.append(context)
        said = " ".join(item["text"] for item in context.latest)
        for marker, turn in self.overrides.items():
            if marker in said:
                return turn
        if self.interrupt_on and self.interrupt_on in said:
            raise ProcessDied
        if "Что должен делать бот" in said:
            return PersonaTurn(
                decision=PersonaDecision.REPLY,
                text="Хочу получать новые посты из каналов @chan_one и @chan_two.",
            )
        if "Какой язык" in said:
            return PersonaTurn(decision=PersonaDecision.REPLY, text="Русский.")
        if "Описание заказа" in said:
            return PersonaTurn(decision=PersonaDecision.REPLY, text="Да, всё верно.")
        return PersonaTurn(decision=PersonaDecision.WAIT)


@dataclass
class Harness:
    clock: FakeClock
    world: World
    telegram: FakeTelegram
    api: FakeApi
    persona: ScriptedPersona
    platform: FakePlatform
    repository: FakeRepository
    store: EvidenceStore

    async def stored_secrets(self, project_id: str) -> dict:
        self.world.events.append(("api", "project_secrets"))
        return dict(self.world.secrets.get(project_id, {}))

    def buyer(self, **config_overrides: Any) -> SyntheticBuyer:
        return SyntheticBuyer(
            parse_config(config_data(**config_overrides)),
            telegram=self.telegram,
            lease=identity_lease(self.world.redis, self.clock),
            api=self.api,
            persona=self.persona,
            platform=self.platform,
            repository=self.repository,
            stored_secrets=self.stored_secrets,
            store=self.store,
            clock=self.clock.clock(),
            environ=ENVIRON,
        )

    def resume(self, directory) -> None:
        """A new process: a fresh redaction set over the retained evidence."""
        self.store = EvidenceStore(directory, Redaction(), clock=self.clock.wall)
        self.store.load()
        self.telegram._connected = False

    def evidence_text(self) -> str:
        return (self.store.directory / "evidence.json").read_text() + (
            self.store.directory / "report.md"
        ).read_text()


def post_event(channel: str, text: str, url: str) -> str:
    """The released `tg-channels.post` event's Russian form (kit 0.1.2 binding)."""
    return f"Новая публикация: @{channel}\n2026-10-10\n{text}\n{url}"


def harness(directory, *, me: int = BUYER) -> Harness:
    clock = FakeClock()
    world = World(clock)
    store = EvidenceStore(directory, Redaction(), clock=clock.wall)
    store.record = new_record(
        operation_id="s1487-buyer-001", revision=REVISION, handles={}, now=START.isoformat()
    )
    return Harness(
        clock=clock,
        world=world,
        telegram=FakeTelegram(world, me=me),
        api=FakeApi(world),
        persona=ScriptedPersona(),
        platform=FakePlatform(world),
        repository=FakeRepository(world),
        store=store,
    )
