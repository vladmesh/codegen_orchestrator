"""An in-process world for the synthetic buyer: Telegram, the Codegen API, the product.

The Codegen bot here answers the way the released PO's scenario does (token, then
project, then the feature, a module route and a language question, the brief, the
accepted order) and records into the fake API exactly what the real one would
expose. Time is a fake clock that only moves when the controller sleeps; native
work, QA runs and channel posts happen on those sleeps. Nothing starts a process,
opens a socket or really sleeps.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from shared.catalog_activation import CATALOG_ACTIVATION
from shared.contracts.dto.capability_preview import CapabilityPlan
from src.synthetic_buyer.codegen_api import ApiRefused
from src.synthetic_buyer.config import parse_config
from src.synthetic_buyer.controller import Clock, SyntheticBuyer
from src.synthetic_buyer.evidence import EvidenceStore, Redaction, new_record
from src.synthetic_buyer.persona import (
    PersonaContext,
    PersonaDecision,
    PersonaTurn,
    StatedRoute,
)
from src.synthetic_buyer.platform_evidence import AuthFacts, UsageFacts
from src.synthetic_buyer.telegram import Button, Message, Peer, TransportError

BUYER = 8202532144
USER_ID = 21
CODEGEN = Peer(id=7001, username="codegen_orch_bot", is_bot=True)
PRODUCT = Peer(id=7002, username="channels_digest_bot", is_bot=True)
BOTFATHER = Peer(id=93372553, username="BotFather", is_bot=True)
STRANGER = 5550001
TOKEN = "7712345678:" + "AAH" + "k" * 32
PROMO = "PROMOCODEVALUE" + "Q" * 10
SESSION = "1BVtsOK" + "s" * 60
API_HASH = "f" * 32
INTERNAL_KEY = "internal-key-value-0123456789"
UNRELATED_PROJECT = "11111111-1111-4111-8111-111111111111"
PROJECT = "22222222-2222-4222-8222-222222222222"
STORY = "story-0001"
BRIEF = "brief-0001"
PREVIEW = "preview-" + "1" * 24
CHANNELS = ["chan_one", "chan_two"]
START = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)
REVISION = "f090f9ad6682c28de4e508a24082f8b2cbcc7770"


def config_data(**overrides: Any) -> dict:
    data = {
        "schema_version": 1,
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
            "qa_quiet_seconds": 600,
            "probe_reply_seconds": 30,
            "post_delivery_seconds": 600,
            "teardown_seconds": 600,
            "poll_seconds": 30,
            "send_attempts": 2,
        },
        "evidence_dir": "/evidence",
        "api": {"base_url": "http://api:8000"},
        "telegram": {
            "api_id": {"env": "TELETHON_API_ID"},
            "api_hash": {"env": "TELETHON_API_HASH"},
            "session": {"env": "TELETHON_SESSION"},
        },
        "registration": {"credits_microusd": 5_000_000, "attempt_reservation_microusd": 500_000},
        "product_token": {"mode": "handle", "handle": {"env": "BUYER_PRODUCT_BOT_TOKEN"}},
        "platform": {
            "auth_admin_url": {"env": "PLATFORM_AUTH_ADMIN_URL"},
            "auth_admin_token": {"env": "PLATFORM_AUTH_ADMIN_TOKEN"},
            "reader_base_url": "https://reader.example.test/tg-reader",
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
    "BUYER_PRODUCT_BOT_TOKEN": TOKEN,
    "PLATFORM_AUTH_ADMIN_URL": "http://auth:8000",
    "PLATFORM_AUTH_ADMIN_TOKEN": "admin-token-value-abcdef",
    "SECRETS_ENCRYPTION_KEY": "runtime-key",
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
    hooks: list[Callable[[], None]] = field(default_factory=list)
    slept: float = 0.0

    def wall(self) -> datetime:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)
        self.slept += seconds
        for hook in list(self.hooks):
            hook()

    def clock(self) -> Clock:
        return Clock(wall=self.wall, sleep=self.sleep)


class World:
    """Both sides of every boundary the controller crosses, in one consistent state."""

    def __init__(self, clock: FakeClock) -> None:
        self.clock = clock
        self.dialogs: dict[int, list[Message]] = {CODEGEN.id: [], PRODUCT.id: [], BOTFATHER.id: []}
        self.next_id = 500
        self.users: dict[int, dict] = {}
        self.promos: list[dict] = []
        self.projects: dict[str, dict] = {
            UNRELATED_PROJECT: {
                "id": UNRELATED_PROJECT,
                "owner_id": 99,
                "created_at": "2026-01-01T00:00:00+00:00",
            }
        }
        self.rollout: dict = {"project_ids": [UNRELATED_PROJECT], "note": "operator-owned"}
        self.rollout_writes: list[dict] = []
        self.previews: dict[str, dict] = {}
        self.stories: list[dict] = []
        self.briefs: dict[str, dict] = {}
        self.plans: dict[str, CapabilityPlan] = {}
        self.tasks: list[dict] = []
        self.runs: list[dict] = []
        self.busy_qa: list[dict] = []
        self.language = "ru"
        self.teardown = "completed"
        self.sends_while_qa_busy: list[str] = []
        self.connects_while_qa_busy = 0
        self.botfather_state: str | None = None
        self.botfather_bots: list[str] = []
        self.botfather_dies_after_creation = False
        self.order_messages = 0
        self.stage = "new"
        self.route = "module"
        self.post_after_digest = True
        self.build_ticks = 2
        self.story_final = "completed"
        self.new_project_owner = USER_ID
        self.auth = AuthFacts("orch-x", "abcdefghijk2", ("abcdefghijk2",), ())
        self.usage = UsageFacts(200, 2, 1, 2)
        self.language_switch_works = True
        self.deploy_project = PROJECT
        self.events: list[tuple] = []

    # --- Telegram side ---------------------------------------------------------

    def append(
        self,
        peer: Peer,
        text: str,
        *,
        sender: int | None = None,
        out: bool = False,
        urls: tuple[str, ...] = (),
        buttons: tuple[Button, ...] = (),
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
        )
        self.dialogs[peer.id].append(message)
        return message

    def codegen_answer(self, text: str) -> list[str]:  # noqa: PLR0911 - the PO scenario
        if text == PROMO:
            self.users[BUYER] = {"id": USER_ID, "telegram_id": BUYER}
            self.promos[-1]["redeemed_by_user_id"] = USER_ID
            return ["Промокод активирован. Добро пожаловать!"]
        if BUYER not in self.users:
            return ["Чтобы начать, пришлите одноразовый промокод."]
        if text == TOKEN:
            self.projects[PROJECT] = {
                "id": PROJECT,
                "owner_id": self.new_project_owner,
                "created_at": self.clock.now.isoformat(),
                "status": "draft",
            }
            self.stage = "project"
            return [f"Проект создан, токен принят (echo {TOKEN}). Что должен делать бот?"]
        self.order_messages += 1
        if self.stage == "new":
            return ["Отлично! Пришлите, пожалуйста, токен бота от @BotFather."]
        if self.stage == "project":
            self.previews[PREVIEW] = {
                "preview_id": PREVIEW,
                "created_at": self.clock.now.isoformat(),
            }
            self.stage = "preview"
            words = {"module": "готовым решением", "from_scratch": "с нуля"}[self.route]
            return [f"Это можно сделать {words}.", "Какой язык бота: русский или английский?"]
        if self.stage == "preview":
            self.stage = "brief"
            return ["Описание заказа: бот присылает посты из каналов. Подтверждаете?"]
        if self.stage == "brief":
            self.stage = "ordered"
            self.stories.append(
                {
                    "id": STORY,
                    "project_id": PROJECT,
                    "status": "in_progress",
                    "created_at": self.clock.now.isoformat(),
                }
            )
            self.briefs[STORY] = {
                "id": BRIEF,
                "revision": 1,
                "confirmed_at": self.clock.now.isoformat(),
                "content": {"language": "ru"},
            }
            self.plans[BRIEF] = plan(self.route)
            self.clock.hooks.append(self.build_tick)
            return ["Заказ принят, приступаем к работе."]
        return ["Работа идёт."]

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

    def product_answer(self, text: str) -> list[tuple[str, tuple[str, ...]]]:
        if self.busy_qa:
            self.sends_while_qa_busy.append(text)
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
            return (
                [(f"@{name}", ()) for name in CHANNELS]
                if ru
                else [(f"Channel @{name}", ()) for name in CHANNELS]
            )
        if text == "/digest":
            if self.post_after_digest:
                self.digest_at = self.clock.now
                self.clock.hooks.append(self.deliver_post)
            url = "https://t.me/chan_one/100"
            return [(f"@chan_one\n2026-10-09\nСтарый пост\n{url}", (url,))]
        return []

    def deliver_post(self) -> None:
        if self.clock.now < self.digest_at + timedelta(seconds=120):
            return
        self.clock.hooks.remove(self.deliver_post)
        url = "https://t.me/chan_two/201"
        self.append(PRODUCT, f"@chan_two\nНовый пост\n{url}", urls=(url,))

    # --- native work ---------------------------------------------------------------

    def build_tick(self) -> None:
        self.build_ticks -= 1
        if self.build_ticks > 0:
            return
        self.clock.hooks.remove(self.build_tick)
        story = self.stories[0]
        story["status"] = self.story_final
        story["generated_product_timeline"] = {
            "pull_request": {
                "number": 1,
                "state": "closed",
                "merged_at": "2026-10-10T13:00:00+00:00",
                "head_sha": "a" * 40,
                "merge_commit_sha": "b" * 40,
            },
            "ci_runs": [
                {"id": 9, "url": "https://ci/9", "conclusion": "success", "head_sha": "a" * 40}
            ],
        }
        self.tasks[:] = [
            {
                "id": "t-install",
                "type": "install",
                "status": "done",
                "blocked_by_task_id": None,
                "created_at": "2026-10-10T12:10:00+00:00",
                "install_operation": {
                    "id": "op-1",
                    "state": "published",
                    "stage": "published",
                    "token": "install-op-secret-token",
                },
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
                "result": {
                    "deploy_outcome": "success",
                    "deployed_url": "https://product.example.test",
                    "application_id": 5,
                    "bot_username": PRODUCT.username,
                    "deployment_result": {
                        "status": "success",
                        "deployed_commit_sha": "b" * 40,
                        "image_references": {"backend": "registry/p@sha256:" + "c" * 64},
                    },
                },
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

    @property
    def connected(self) -> bool:
        return self._connected

    def _require(self) -> None:
        if not self._connected:
            raise TransportError("session", "not connected")

    async def connect(self) -> None:
        if self.world.busy_qa:
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
        return {p.username.casefold(): p for p in (CODEGEN, PRODUCT, BOTFATHER)}[
            username.casefold()
        ]

    async def latest_id(self, peer: Peer) -> int:
        self._require()
        dialog = self.world.dialogs[peer.id]
        return dialog[-1].id if dialog else 0

    async def messages_after(self, peer: Peer, after_id: int) -> list[Message]:
        self._require()
        found = [m for m in self.world.dialogs[peer.id] if m.id > after_id]
        return found + found if self.duplicate_reads else found

    async def send(self, peer: Peer, text: str) -> Message:
        self._require()
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
        return sent

    async def press(self, peer: Peer, message_id: int, data: bytes) -> None:
        self._require()
        self.events.append(("press", peer.username, message_id, data))


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
        promo = {"id": len(self.world.promos) + 1, "code": PROMO, "redeemed_by_user_id": None}
        self.world.promos.append(promo)
        return promo

    async def owned_projects(self, telegram_id: int) -> list[dict]:
        self._call("owned_projects")
        user = self.world.users[telegram_id]
        return [
            p
            for p in self.world.projects.values()
            if p["owner_id"] == user["id"]
            or (p["id"] == PROJECT and self.world.new_project_owner != USER_ID)
        ]

    async def project(self, project_id: str, *, as_user: int | None = None) -> dict:
        self._call("project")
        return dict(self.world.projects[project_id])

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
        return self.world.briefs.get(story_id)

    async def capability_plan(self, brief_id: str) -> CapabilityPlan | None:
        self._call("capability_plan")
        return self.world.plans.get(brief_id)

    async def capability_preview(self, preview_id: str) -> dict:
        self._call("capability_preview")
        return self.world.previews[preview_id]

    async def tasks(self, story_id: str) -> list[dict]:
        self._call("tasks")
        return [dict(t) for t in self.world.tasks]

    async def runs(self, *, story_id=None, run_type=None, status=None) -> list[dict]:
        self._call("runs")
        if story_id is None:
            return [r for r in self.world.busy_qa if r["status"] == status]
        return [
            r for r in self.world.runs if r["type"] == run_type.value and r["story_id"] == story_id
        ]

    async def bot_liveness(self, project_id: str, telegram_id: int) -> dict:
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
        if "токен" in said and "принят" not in said:
            return PersonaTurn(decision=PersonaDecision.WAIT, bot_asks_for_token=True)
        if "Что должен делать бот" in said:
            return PersonaTurn(
                decision=PersonaDecision.REPLY,
                text="Хочу получать новые посты из каналов @chan_one и @chan_two.",
            )
        if "готовым решением" in said:
            return PersonaTurn(
                decision=PersonaDecision.REPLY, text="Русский.", stated_route=StatedRoute.MODULE
            )
        if "с нуля" in said:
            return PersonaTurn(
                decision=PersonaDecision.REPLY,
                text="Русский.",
                stated_route=StatedRoute.FROM_SCRATCH,
            )
        if "Описание заказа" in said:
            return PersonaTurn(
                decision=PersonaDecision.REPLY,
                text="Да, всё верно.",
                brief_presented=True,
                confirms_brief=True,
            )
        if not context.latest:
            return PersonaTurn(
                decision=PersonaDecision.REPLY,
                text="Здравствуйте! Хочу заказать нового Telegram-бота.",
            )
        return PersonaTurn(decision=PersonaDecision.WAIT)


@dataclass
class Harness:
    clock: FakeClock
    world: World
    telegram: FakeTelegram
    api: FakeApi
    persona: ScriptedPersona
    platform: FakePlatform
    store: EvidenceStore

    def buyer(self, **config_overrides: Any) -> SyntheticBuyer:
        return SyntheticBuyer(
            parse_config(config_data(**config_overrides)),
            telegram=self.telegram,
            api=self.api,
            persona=self.persona,
            platform=self.platform,
            store=self.store,
            clock=self.clock.clock(),
            environ=ENVIRON,
        )

    def evidence_text(self) -> str:
        return (self.store.directory / "evidence.json").read_text() + (
            self.store.directory / "report.md"
        ).read_text()


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
        store=store,
    )
