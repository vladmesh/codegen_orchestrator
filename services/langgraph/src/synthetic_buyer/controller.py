"""One synthetic purchase, from the customer's first message to the project's teardown.

The controller is deterministic and owns every decision: which session is
connected, what is sent, when to pause, when to stop. The persona only proposes
the customer's words. The order of an operation is fixed:

1. prove the session is the configured buyer before anything is sent, and that the
   bot it talks to is the configured Codegen bot;
2. register the buyer through the customer door (a promo code minted by the
   internal API, redeemed by sending it to the Codegen bot) or reuse the buyer
   the API already knows;
3. obtain the product bot token (a protected handle, or a bot this operation
   creates in BotFather);
4. order through the actual Codegen bot. When the API shows the order's new
   project, the conversation pauses: the project's id is appended to
   `capabilities.module_rollout` and read back before the customer describes
   the channel feature, and the brief is confirmed only after the bot stated a
   module route;
5. disconnect the shared session and observe the native work through the API
   until the story is completed, its deploy succeeded and its QA passed;
6. wait until no QA run holds the shared account, reconnect, and probe the live
   product as the customer: Russian replies, `/channels`, `/digest`, a post
   delivered unsolicited from a configured channel, the language switched through
   core settings and an English reply; read the platform's auth and reader facts;
7. freeze the verdict, then tear down only this operation's project.

Every phase boundary writes the redacted evidence, so `resume` and `cleanup`
continue from retained ids and never order, register or create a bot twice.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
import hashlib
import re
from typing import Any

from shared.contracts.dto.run import RunType
from shared.contracts.dto.story import StoryStatus

from . import native, probe
from .codegen_api import ApiRefused, CodegenApi, project_ids
from .config import BuyerConfig, MissingSecret, resolve_secret
from .evidence import (
    CleanupStatus,
    EvidenceStore,
    ObservationStatus,
    Phase,
    VerdictStatus,
)
from .persona import (
    ACCEPTED_ROUTES,
    Persona,
    PersonaContext,
    PersonaDecision,
    PersonaInvalid,
    PersonaTurn,
    StatedRoute,
    deviation,
)
from .platform_evidence import PlatformFacts
from .telegram import Message, Peer, TelegramPort, TransportError

#: How often an open dialog is re-read while a reply is awaited.
DIALOG_POLL_SECONDS = 2
#: How much of the dialog the persona is shown.
PERSONA_TRANSCRIPT = 40
BOT_TOKEN = re.compile(r"\d{6,12}:[A-Za-z0-9_-]{30,}")
SHOWN = {
    "promo_code": "[покупатель отправил промокод]",
    "product_token": "[покупатель отправил токен бота]",
}
OPENING = (
    "Начни разговор: скажи, что хочешь заказать нового Telegram-бота для себя. Пока бот "
    "не сообщил, что проект создан, не описывай, что именно должен делать бот: если спросят, "
    "ответь, что подробно опишешь сразу после создания проекта."
)
DESCRIBE = (
    "Проект создан. Теперь опиши, что должен делать бот, отвечай на вопросы, выбирай русский "
    "язык, соглашайся на разбиение на этапы и подтверди описание заказа, если оно совпадает с "
    "твоим желанием."
)
_STOPPED_STORY = {
    StoryStatus.FAILED.value,
    StoryStatus.WAITING_HUMAN_REVIEW.value,
    StoryStatus.WAITING_USER_SECRET.value,
    StoryStatus.ARCHIVED.value,
}


class Stop(Exception):  # noqa: N818 - the controller's one way to end a phase
    """The operation cannot go on; `reason` names why, `detail` is non-secret evidence."""

    def __init__(self, reason: str, detail: Any = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.detail = detail


@dataclass
class Clock:
    """Wall time for deadlines that survive a resume, and the one way to wait."""

    wall: Callable[[], datetime]
    sleep: Callable[[float], Awaitable[None]]


@dataclass(frozen=True)
class Outcome:
    verdict: str
    cleanup: str

    @property
    def exit_code(self) -> int:
        passed = self.verdict == VerdictStatus.PASSED.value
        return 0 if passed and self.cleanup == CleanupStatus.COMPLETED.value else 1


def botfather_username(operation_id: str) -> str:
    """The one bot username this operation may create: deterministic, so never twice."""
    return "sb" + hashlib.sha256(operation_id.encode()).hexdigest()[:12] + "_bot"


class SyntheticBuyer:
    """The controller of one operation over injected ports."""

    def __init__(  # noqa: PLR0913 - every port is explicit
        self,
        config: BuyerConfig,
        *,
        telegram: TelegramPort,
        api: CodegenApi,
        persona: Persona,
        platform: PlatformFacts,
        store: EvidenceStore,
        clock: Clock,
        environ: Mapping[str, str],
    ) -> None:
        self.config = config
        self.telegram = telegram
        self.api = api
        self.persona = persona
        self.platform = platform
        self.store = store
        self.clock = clock
        self.environ = environ
        self._token: str | None = None
        self._peers: dict[str, Peer] = {}

    # --- evidence shorthands --------------------------------------------------

    @property
    def ids(self) -> dict:
        return self.store.record["ids"]

    @property
    def buyer(self) -> int:
        return self.config.buyer.telegram_id

    def _section(self, name: str) -> dict:
        return self.store.record.setdefault(name, {})

    def _elapsed_since(self, stamp: str) -> float:
        return (self.clock.wall() - datetime.fromisoformat(stamp)).total_seconds()

    # --- the operation --------------------------------------------------------

    def _phases(self) -> list[tuple[Phase, Callable[[], Awaitable[None]]]]:
        return [
            (Phase.PREFLIGHT, self._preflight),
            (Phase.REGISTRATION, self._registration),
            (Phase.PRODUCT_TOKEN, self._product_token_phase),
            (Phase.ORDER, self._order),
            (Phase.HANDOFF, self._handoff),
            (Phase.BUILD, self._build),
            (Phase.QA_QUIET, self._wait_qa_quiet),
            (Phase.PRODUCT_PROBE, self._product_probe),
            (Phase.PLATFORM, self._platform),
            (Phase.LANGUAGE, self._language),
            (Phase.AUTH_RECHECK, self._auth_recheck),
            (Phase.FREEZE, self._freeze),
        ]

    async def run(self) -> Outcome:
        """Run or resume the acceptance phases, then tear down what this operation owns."""
        if self.store.record["verdict"]["status"] != VerdictStatus.FAILED.value:
            phase = Phase.PREFLIGHT
            try:
                for phase, step in self._phases():
                    if self.store.is_complete(phase):
                        continue
                    self.store.enter(phase)
                    await step()
                    self.store.complete(phase)
            except Stop as stop:
                self.store.fail(phase, stop.reason, self.store.redaction.value(stop.detail))
            except (TransportError, ApiRefused, MissingSecret, PersonaInvalid) as error:
                self.store.fail(phase, type(error).__name__, self.store.redaction.text(str(error)))
            except Exception as error:  # noqa: BLE001 - the text may hold what it was handed
                self.store.fail(phase, "unexpected_error", {"type": type(error).__name__})
            finally:
                await self._release_session()
        return Outcome(self.store.record["verdict"]["status"], await self.cleanup())

    async def _release_session(self) -> None:
        if self.telegram.connected:
            try:
                await self.telegram.disconnect()
            except TransportError as error:
                self.store.decide({"event": "disconnect_failed", "stage": error.stage})
        self._section("session")["released_at"] = self.store.now()
        self.store.save()

    # --- the session ------------------------------------------------------------

    async def _connect(self) -> None:
        """Connect and prove the buyer before anything may be sent."""
        if not self.telegram.connected:
            await self.telegram.connect()
            self._section("session")["connected_at"] = self.store.now()
        me = await self.telegram.me()
        if me != self.buyer:
            await self.telegram.disconnect()
            raise Stop("identity_mismatch", {"expected": self.buyer, "actual": me})
        self.ids["buyer_telegram_id"] = me

    async def _peer(self, role: str, username: str, *, user_id: int | None = None) -> Peer:
        if role in self._peers:
            return self._peers[role]
        peer = await self.telegram.resolve(username)
        if (
            not peer.is_bot
            or peer.username.casefold() != username.casefold()
            or (user_id is not None and peer.id != user_id)
        ):
            raise Stop(f"{role}_bot_mismatch", {"username": username, "resolved_id": peer.id})
        self._peers[role] = peer
        self.ids[f"{role}_bot_id"] = peer.id
        return peer

    async def _codegen(self) -> Peer:
        await self._connect()
        bot = self.config.codegen_bot
        return await self._peer("codegen", bot.username, user_id=bot.user_id)

    async def _ensure_watermark(self, peer: Peer, dialog: str) -> None:
        marks = self.store.record["watermarks"]
        if dialog not in marks:
            marks[dialog] = await self.telegram.latest_id(peer)
            self.store.save()

    async def _read_new(self, peer: Peer, dialog: str) -> list[Message]:
        """Inbound messages from *peer* above the dialog's watermark, each read once."""
        marks = self.store.record["watermarks"]
        mark = marks[dialog]
        inbound = []
        for message in await self.telegram.messages_after(peer, mark):
            if message.id <= mark:
                continue
            mark = max(mark, message.id)
            if message.outgoing:
                continue
            if message.sender_id != peer.id:
                self.store.decide({"event": "unrelated_message_ignored", "dialog": dialog})
                continue
            inbound.append(message)
            self.store.message(
                dialog, {"direction": "in", **message.evidence(self.store.redaction)}
            )
        if mark != marks[dialog]:
            marks[dialog] = mark
            self.store.save()
        return inbound

    async def _await_reply(self, peer: Peer, dialog: str, *, timeout: float) -> list[Message]:
        """What *peer* answers: every message until it has been quiet for the settle time."""
        settle = self.config.deadlines.settle_seconds
        started = self.clock.wall()
        last_new = started
        collected: list[Message] = []
        while True:
            new = await self._read_new(peer, dialog)
            now = self.clock.wall()
            if new:
                collected += new
                last_new = now
            waited = (now - started).total_seconds()
            if collected and ((now - last_new).total_seconds() >= settle or waited >= 2 * timeout):
                return collected
            if not collected and waited >= timeout:
                return []
            await self.clock.sleep(DIALOG_POLL_SECONDS)

    async def _send(
        self, peer: Peer, dialog: str, text: str, *, kind: str, shown: str | None = None
    ) -> Message:
        """Send once; a send whose delivery is unknown is looked for before it is retried."""
        marks = self.store.record["watermarks"]
        failures = []
        for _ in range(self.config.deadlines.send_attempts):
            try:
                sent = await self.telegram.send(peer, text)
            except TransportError as error:
                failures.append(error.stage)
                try:
                    recent = await self.telegram.messages_after(peer, marks[dialog])
                except TransportError:
                    recent = []
                delivered = [item for item in recent if item.outgoing and item.text == text]
                if not delivered:
                    await self.clock.sleep(DIALOG_POLL_SECONDS)
                    continue
                sent = delivered[0]
            entry = {
                "direction": "out",
                "kind": kind,
                "id": sent.id,
                "date": sent.date.isoformat(),
                "text": shown if shown is not None else self.store.redaction.text(text),
                "send_failures": failures,
            }
            self.store.message(dialog, entry)
            marks[dialog] = max(marks[dialog], sent.id)
            self.store.save()
            return sent
        raise Stop("send_failed", {"dialog": dialog, "kind": kind, "failures": failures})

    # --- 1. preflight -----------------------------------------------------------

    async def _preflight(self) -> None:
        await self._codegen()
        self.store.stamp("identity_proven_at")

    # --- 2. registration --------------------------------------------------------

    async def _registration(self) -> None:
        registration = self._section("registration")
        user = await self.api.user_by_telegram(self.buyer)
        if user is not None:
            self.ids["user_id"] = user["id"]
            # A buyer this operation registered before an interruption stays "redeemed".
            registration.setdefault(
                "mode", "redeemed" if registration.get("promo_code_ids") else "reused"
            )
            return
        peer = await self._codegen()
        await self._ensure_watermark(peer, "codegen")
        policy = self.config.registration
        promo = await self.api.mint_promo(
            credits_microusd=policy.credits_microusd,
            reservation_microusd=policy.attempt_reservation_microusd,
        )
        self.store.redaction.add(promo["code"])
        registration.setdefault("promo_code_ids", []).append(promo["id"])
        self.store.save()
        await self._send(
            peer, "codegen", promo["code"], kind="promo_code", shown=SHOWN["promo_code"]
        )
        replies = await self._await_reply(
            peer, "codegen", timeout=self.config.deadlines.reply_seconds
        )
        user = await self.api.user_by_telegram(self.buyer)
        if user is None:
            raise Stop("registration_refused", {"replies": probe.texts(replies)})
        self.ids["user_id"] = user["id"]
        registration["mode"] = "redeemed"
        registration["promo_code_id"] = promo["id"]

    # --- 3. product token -------------------------------------------------------

    async def _product_token_phase(self) -> None:
        await self._product_token()
        self._section("product_token")["source"] = (
            self.config.product_token.handle.describe()
            if self.config.product_token.handle is not None
            else f"botfather:@{self.ids.get('botfather_bot_username')}"
        )

    async def _product_token(self) -> str:
        if self._token is not None:
            return self._token
        source = self.config.product_token
        if source.handle is not None:
            token = resolve_secret(source.handle, self.environ)
            self.store.redaction.add(token)
            if not BOT_TOKEN.fullmatch(token):
                raise Stop("product_token_malformed", {"handle": source.handle.describe()})
        else:
            token = await self._botfather_token()
        self._token = token
        return token

    async def _botfather_exchange(self, peer: Peer, text: str) -> list[Message]:
        await self._send(peer, "botfather", text, kind="botfather")
        return await self._await_reply(
            peer, "botfather", timeout=self.config.deadlines.reply_seconds
        )

    @staticmethod
    def _token_in(replies: list[Message]) -> str | None:
        for message in replies:
            found = BOT_TOKEN.search(message.text)
            if found:
                return found.group(0)
        return None

    async def _botfather_token(self) -> str:
        await self._connect()
        source = self.config.product_token
        peer = await self._peer("botfather", source.botfather_username or "")
        await self._ensure_watermark(peer, "botfather")
        state = self._section("botfather")
        username = botfather_username(self.config.operation_id)
        if state.get("requested_username"):
            # A crash after asking may have created it: never create a second one.
            await self._botfather_exchange(peer, "/token")
            token = self._token_in(await self._botfather_exchange(peer, f"@{username}"))
            if token is not None:
                self.store.redaction.add(token)
                state["created_username"] = username
                self.ids["botfather_bot_username"] = username
                self.store.save()
                return token
            if state.get("created_username"):
                raise Stop("botfather_token_unavailable", {"username": username})
        state["requested_username"] = username
        self.store.save()
        await self._botfather_exchange(peer, "/newbot")
        await self._botfather_exchange(peer, source.bot_display_name or "")
        replies = await self._botfather_exchange(peer, username)
        token = self._token_in(replies)
        if token is None:
            raise Stop("botfather_refused", {"username": username, "replies": probe.texts(replies)})
        self.store.redaction.add(token)
        state["created_username"] = username
        self.ids["botfather_bot_username"] = username
        self.store.save()
        return token

    # --- 4. the order -----------------------------------------------------------

    async def _order(self) -> None:  # noqa: C901, PLR0912 - one bounded dialog loop
        peer = await self._codegen()
        await self._ensure_watermark(peer, "codegen")
        order = self._section("order")
        if "baseline_project_ids" not in order:
            owned = await self.api.owned_projects(self.buyer)
            order["baseline_project_ids"] = sorted(str(project["id"]) for project in owned)
            order["started_at"] = self.store.now()
            order["conversation_from"] = len(self.store.record["conversation"]["codegen"])
            self.store.save()
        deadline = self.config.deadlines.order_seconds
        # A resumed order first answers what the bot said before the interruption.
        latest = self._unanswered() + await self._read_new(peer, "codegen")
        expect_reply = False
        while True:
            await self._observe_project()
            if await self._observe_story():
                await self._await_reply(
                    peer, "codegen", timeout=self.config.deadlines.settle_seconds
                )
                self.store.stamp("order_accepted_at")
                return
            if self._elapsed_since(order["started_at"]) >= deadline:
                raise Stop("order_timeout", {"seconds": deadline})
            if expect_reply:
                latest = await self._await_reply(
                    peer, "codegen", timeout=self.config.deadlines.reply_seconds
                )
                expect_reply = False
                if not latest:
                    await self._observe_project()
                    if await self._observe_story():
                        continue
                    raise Stop("conversation_stalled", self._last_turns())
                continue
            turn = await self.persona.turn(self._persona_context(latest))
            self.store.decide({"event": "persona_turn", **turn.model_dump(mode="json")})
            await self._apply(peer, turn, latest)
            latest = []
            expect_reply = True

    def _unanswered(self) -> list[Message]:
        """Inbound messages retained after the buyer's last action: read, never answered."""
        unanswered: list[Message] = []
        start = self._section("order")["conversation_from"]
        for entry in self.store.record["conversation"]["codegen"][start:]:
            if entry["direction"] == "out":
                unanswered = []
                continue
            unanswered.append(
                Message(
                    id=entry["id"],
                    sender_id=entry["sender_id"],
                    outgoing=False,
                    date=datetime.fromisoformat(entry["date"]),
                    text=entry["text"],
                )
            )
        return unanswered

    def _persona_context(self, latest: list[Message]) -> PersonaContext:
        order = self._section("order")
        transcript = [
            {"from": "bot" if entry["direction"] == "in" else "me", "text": entry.get("text", "")}
            for entry in self.store.record["conversation"]["codegen"][-PERSONA_TRANSCRIPT:]
        ]
        return PersonaContext(
            instruction=DESCRIBE if order.get("allowlist_applied_at") else OPENING,
            transcript=transcript,
            latest=[
                {
                    "text": self.store.redaction.text(message.text),
                    "buttons": [self.store.redaction.text(b.text) for b in message.buttons],
                }
                for message in latest
            ],
        )

    async def _apply(self, peer: Peer, turn: PersonaTurn, latest: list[Message]) -> None:  # noqa: C901
        order = self._section("order")
        reason = deviation(turn, self.config.scenario)
        if reason is not None:
            raise Stop(
                "persona_deviation",
                {"reason": reason, "text": self.store.redaction.text(turn.text or "")},
            )
        if turn.stated_route is not StatedRoute.NOT_STATED:
            order["stated_route"] = turn.stated_route.value
            if turn.stated_route not in ACCEPTED_ROUTES:
                raise Stop("route_not_module", {"stated_route": turn.stated_route.value})
        if turn.decision is PersonaDecision.IMPOSSIBLE:
            raise Stop("order_impossible", {"bot": probe.texts(latest)})
        if turn.bot_asks_for_token:
            if order.get("token_sent_at"):
                raise Stop(
                    "product_token_rejected",
                    {
                        "source": self._section("product_token").get("source"),
                        "bot": probe.texts(latest),
                    },
                )
            token = await self._product_token()
            await self._send(
                peer, "codegen", token, kind="product_token", shown=SHOWN["product_token"]
            )
            order["token_sent_at"] = self.store.now()
            return
        if turn.decision is PersonaDecision.WAIT:
            return
        turns = order["turns"] = order.get("turns", 0) + 1
        if turns > self.config.deadlines.conversation_turns:
            raise Stop("turn_limit", {"turns": turns - 1})
        if turn.confirms_brief:
            route = order.get("stated_route")
            if not order.get("allowlist_applied_at") or route not in {
                r.value for r in ACCEPTED_ROUTES
            }:
                raise Stop(
                    "confirmation_not_admitted",
                    {
                        "allowlist_applied": bool(order.get("allowlist_applied_at")),
                        "stated_route": route,
                    },
                )
            order["confirmed_at"] = self.store.now()
        if turn.decision is PersonaDecision.PRESS:
            for message in reversed(latest):
                for button in message.buttons:
                    if button.text == turn.button and button.data is not None:
                        await self.telegram.press(peer, message.id, button.data)
                        self.store.message(
                            "codegen",
                            {
                                "direction": "out",
                                "kind": "press",
                                "message_id": message.id,
                                "text": self.store.redaction.text(button.text),
                            },
                        )
                        self.store.save()
                        return
            raise Stop("button_not_visible", {"button": turn.button})
        await self._send(peer, "codegen", turn.text or "", kind="persona")

    def _last_turns(self) -> dict:
        return {"last": self.store.record["conversation"]["codegen"][-4:]}

    async def _observe_project(self) -> None:
        """Pause on the order's new project: prove it is ours, then admit it to the rollout."""
        if "project_id" in self.ids:
            if not self._section("order").get("allowlist_applied_at"):
                await self._allowlist(self.ids["project_id"])
            return
        order = self._section("order")
        owned = await self.api.owned_projects(self.buyer)
        new = [p for p in owned if str(p["id"]) not in order["baseline_project_ids"]]
        if not new:
            return
        if len(new) > 1:
            raise Stop("ambiguous_new_projects", {"project_ids": sorted(str(p["id"]) for p in new)})
        project_id = str(new[0]["id"])
        readback = await self.api.project(project_id, as_user=self.buyer)
        if readback.get("owner_id") != self.ids.get("user_id"):
            raise Stop("project_not_owned", {"project_id": project_id})
        created = native.parse_time(readback.get("created_at"))
        started = datetime.fromisoformat(order["started_at"])
        if created is None or created < started - timedelta(seconds=1):
            raise Stop("project_predates_order", {"project_id": project_id})
        self.ids["project_id"] = project_id
        self.store.stamp("project_detected_at")
        self.store.save()
        await self._allowlist(project_id)

    async def _allowlist(self, project_id: str) -> None:
        """Append only this project's id to the module rollout, preserving the rest."""
        before = await self.api.module_rollout()
        prior = project_ids(before)
        if project_id not in prior:
            await self.api.write_module_rollout(
                {**before, "project_ids": [*before.get("project_ids", []), project_id]}
            )
        after = await self.api.module_rollout()
        kept = {key: value for key, value in before.items() if key != "project_ids"}
        if (
            project_id not in project_ids(after)
            or not set(prior) <= set(project_ids(after))
            or any(after.get(key) != value for key, value in kept.items())
        ):
            raise Stop("allowlist_readback", {"project_id": project_id})
        order = self._section("order")
        order["allowlist_applied_at"] = self.store.now()
        order["allowlist_already_present"] = project_id in prior
        self.store.save()

    async def _observe_story(self) -> bool:
        if "story_id" in self.ids:
            return True
        if "project_id" not in self.ids:
            return False
        confirmed = []
        for story in await self.api.stories(self.ids["project_id"]):
            brief = await self.api.brief_by_story(story["id"])
            if brief is not None and brief.get("confirmed_at"):
                confirmed.append((story.get("created_at") or "", story["id"], brief["id"]))
        if not confirmed:
            return False
        confirmed.sort()
        _, self.ids["story_id"], self.ids["brief_id"] = confirmed[0]
        self.ids["other_story_ids"] = [story_id for _, story_id, _ in confirmed[1:]]
        self.store.save()
        return True

    # --- 5. handoff and native work ----------------------------------------------

    async def _handoff(self) -> None:
        await self._release_session()
        self.store.stamp("session_handed_to_qa_at")

    async def _build(self) -> None:
        story_id = self.ids["story_id"]
        started = self._section("order")["started_at"]
        while True:
            story = await self.api.story(story_id)
            status = story.get("status")
            build = self._section("build")
            if build.get("last_status") != status:
                build["last_status"] = status
                build.setdefault("statuses", []).append({"status": status, "at": self.store.now()})
                self.store.save()
            if status == StoryStatus.COMPLETED.value:
                break
            if status in _STOPPED_STORY:
                raise Stop(
                    "story_stopped",
                    {
                        "status": status,
                        "waiting_on": story.get("waiting_on"),
                        "quarantine_reason": story.get("quarantine_reason"),
                    },
                )
            if self._elapsed_since(started) >= self.config.deadlines.build_seconds:
                raise Stop("build_timeout", {"status": status})
            await self.clock.sleep(self.config.deadlines.poll_seconds)
        self.store.stamp("story_completed_at")
        await self._observe_native(story)

    async def _observe_native(self, story: dict) -> None:
        project_id, story_id = self.ids["project_id"], self.ids["story_id"]
        brief = await self.api.brief_by_story(story_id)
        self._record("brief_frozen", native.brief_frozen(brief), "GET /api/product-briefs/by-story")
        plan = await self.api.capability_plan(brief["id"]) if brief else None
        self._record("capability_plan_module", native.plan_routes(plan), "GET capability-plan")
        preview = await self.api.capability_preview(plan.preview_id) if plan else None
        self._record(
            "preview_after_allowlist",
            native.preview_after_allowlist(
                preview, self._section("order").get("allowlist_applied_at")
            ),
            "GET /api/capability-previews/{id} vs. rollout readback",
        )
        tasks = await self.api.tasks(story_id)
        engineering = await self.api.runs(story_id=story_id, run_type=RunType.ENGINEERING)
        self.ids["task_ids"] = [task["id"] for task in tasks]
        self._record(
            "install_after_scaffold",
            native.install_mechanical(tasks, engineering),
            "GET /api/tasks (install_operation) and engineering runs",
        )
        self._record("engineering_glue_only", native.glue_only(tasks), "GET /api/tasks blocked_by")
        self._record("product_ci", native.product_ci(story), "story generated_product_timeline")
        deploys = await self.api.runs(story_id=story_id, run_type=RunType.DEPLOY)
        deploy_finding, deploy = native.deploy_success(project_id, story_id, deploys)
        self._record("deploy_success", deploy_finding, "GET /api/runs type=deploy typed result")
        qa_runs = await self.api.runs(story_id=story_id, run_type=RunType.QA)
        self._record(
            "qa_passed",
            native.qa_passed(project_id, story_id, qa_runs, deploy),
            "GET /api/runs type=qa typed result",
        )
        if deploy is not None:
            self.ids.update(
                deploy_run_id=deploy["run_id"],
                application_id=deploy["application_id"],
                product_bot_username=deploy["bot_username"],
                deployed_url=deploy["deployed_url"],
            )
            liveness = await self.api.bot_liveness(project_id, self.buyer)
            bound = (
                liveness.get("state") == "alive"
                and str(liveness.get("bot_username", "")).casefold()
                == deploy["bot_username"].casefold()
            )
            self._record(
                "product_bot_bound",
                native.observed(liveness) if bound else native.failed(liveness),
                "GET /api/projects/{id}/telegram/liveness",
            )
        qa = self.store.record["observations"].get("qa_passed", {}).get("detail") or {}
        if isinstance(qa, dict) and qa.get("run_id"):
            self.ids["qa_run_id"] = qa["run_id"]
        settled = [
            self.store.record["observations"].get(name, {}).get("status")
            for name in ("deploy_success", "qa_passed", "product_bot_bound")
        ]
        if settled != [ObservationStatus.OBSERVED.value] * 3:
            raise Stop("native_acceptance_not_settled", {"statuses": settled})

    def _record(self, name: str, finding: native.Finding, provenance: str) -> None:
        self.store.observe(name, finding.status, provenance=provenance, detail=finding.detail)

    # --- 6. the shared account and the live product --------------------------------

    async def _busy_qa(self) -> list[str]:
        busy = []
        for status in ("running", "queued"):
            busy += [run["id"] for run in await self.api.runs(run_type=RunType.QA, status=status)]
        return sorted(busy)

    async def _wait_qa_quiet(self) -> None:
        started = self.clock.wall()
        while busy := await self._busy_qa():
            if (
                self.clock.wall() - started
            ).total_seconds() >= self.config.deadlines.qa_quiet_seconds:
                raise Stop("qa_never_quiet", {"qa_run_ids": busy})
            await self.clock.sleep(self.config.deadlines.poll_seconds)
        self.store.stamp("qa_quiet_at")

    async def _guard_qa(self) -> None:
        """Never send while a QA run may hold the same account: step out and come back."""
        busy = await self._busy_qa()
        if not busy:
            return
        self.store.decide({"event": "qa_conflict_yielded", "qa_run_ids": busy})
        await self._release_session()
        await self._wait_qa_quiet()
        await self._connect()

    async def _product(self) -> Peer:
        await self._connect()
        peer = await self._peer("product", self.ids["product_bot_username"])
        await self._ensure_watermark(peer, "product")
        return peer

    async def _command(self, peer: Peer, command: str) -> list[Message]:
        await self._guard_qa()
        await self._send(peer, "product", command, kind="probe")
        return await self._await_reply(
            peer, "product", timeout=self.config.deadlines.probe_reply_seconds
        )

    async def _product_probe(self) -> None:
        await self._wait_qa_quiet()
        peer = await self._product()
        channels = self.config.scenario.public_channels
        started = await self._command(peer, "/start")
        if probe.access_denied(started):
            self._record("reply_ru", native.failed(probe.texts(started)), "product /start")
            raise Stop("product_access_denied", {"replies": probe.texts(started)})
        self._record(
            "reply_ru",
            native.observed(probe.texts(started))
            if probe.answered_in_russian(started)
            else native.failed(probe.texts(started)),
            "product /start",
        )
        listed = await self._command(peer, "/channels")
        missing = probe.missing_channels(listed, channels)
        self._record(
            "channels_listed",
            native.failed({"missing": missing})
            if missing or not listed
            else native.observed(probe.texts(listed)),
            "product /channels",
        )
        digest = await self._command(peer, "/digest")
        digest_urls = {url for message in digest for url in message.urls}
        self._record(
            "digest_answered",
            native.observed({"replies": len(digest), "urls": sorted(digest_urls)})
            if digest
            else native.failed("no reply to /digest"),
            "product /digest",
        )
        await self._release_session()
        await self._await_post(digest_urls)

    async def _await_post(self, digest_urls: set[str]) -> None:
        """A post the product sends on its own, from a configured channel, not in the digest.

        The session is connected only for each read, after the QA check: a long wait
        never holds the account a QA run may need.
        """
        channels = self.config.scenario.public_channels
        started = self.clock.wall()
        while (
            self.clock.wall() - started
        ).total_seconds() < self.config.deadlines.post_delivery_seconds:
            await self._wait_qa_quiet()
            peer = await self._product()
            arrived = await self._read_new(peer, "product")
            await self._release_session()
            for message in arrived:
                fresh = [
                    link
                    for link in probe.channel_post_links(message, channels)
                    if link[2] not in digest_urls
                ]
                if fresh:
                    channel, post_id, url = fresh[0]
                    self._record(
                        "post_delivered",
                        native.observed(
                            {
                                "message_id": message.id,
                                "delivered_at": message.date.isoformat(),
                                "channel": channel,
                                "post_id": post_id,
                                "url": url,
                            }
                        ),
                        "unsolicited product message linking a configured channel post",
                    )
                    return
            await self.clock.sleep(self.config.deadlines.poll_seconds)
        self._record(
            "post_delivered",
            native.unknown(
                f"no unsolicited post within {self.config.deadlines.post_delivery_seconds}s"
            ),
            "unsolicited product message linking a configured channel post",
        )

    async def _auth_finding(self) -> native.Finding:
        try:
            facts = await self.platform.auth(self.ids["project_id"])
        except Exception as error:  # noqa: BLE001 - an unreadable fact stays unknown
            return native.unknown(self.store.redaction.text(str(error)))
        detail = {
            "product_id": facts.product_id,
            "stored_key_id": facts.stored_key_id,
            "active_key_ids": list(facts.active_key_ids),
            "revoked_key_ids": list(facts.revoked_key_ids),
        }
        return native.observed(detail) if facts.stored_key_active else native.failed(detail)

    async def _platform(self) -> None:
        await self._release_session()
        self._record(
            "auth_key_active", await self._auth_finding(), "platform auth admin product keys"
        )
        try:
            usage = await self.platform.usage(
                self.ids["project_id"], self.config.platform.reader_base_url
            )
        except Exception as error:  # noqa: BLE001 - an unreadable fact stays unknown
            self._record(
                "reader_usage",
                native.unknown(self.store.redaction.text(str(error))),
                "reader /v1/usage",
            )
            return
        detail = {
            "status": usage.status,
            "channels_used": usage.channels_used,
            "requests_this_minute": usage.requests_this_minute,
            "resolves_today": usage.resolves_today,
        }
        activity = (usage.requests_this_minute or 0) + (usage.resolves_today or 0)
        if usage.status != 200:  # noqa: PLR2004
            finding = native.failed(detail)
        elif (usage.channels_used or 0) >= 1 and activity >= 1:
            finding = native.observed(detail)
        else:
            finding = native.unknown(detail)
        self._record("reader_usage", finding, "reader GET /v1/usage with the product's own key")

    async def _language(self) -> None:
        language = self.config.scenario.switch_language
        try:
            switched = await self.platform.switch_language(
                self.ids["project_id"], self.ids["deployed_url"], language
            )
        except Exception as error:  # noqa: BLE001 - an unreadable fact stays unknown
            self._record(
                "language_switched",
                native.unknown(self.store.redaction.text(str(error))),
                "core settings",
            )
            return
        self._record(
            "language_switched",
            native.observed({"language": language})
            if switched
            else native.failed({"language": language}),
            "core POST /settings/set then /settings/get",
        )
        if not switched:
            return
        peer = await self._product()
        replies = await self._command(peer, "/channels")
        await self._release_session()
        self._record(
            "reply_en",
            native.observed(probe.texts(replies))
            if probe.answered_in_english(replies)
            else native.failed(probe.texts(replies)),
            "product /channels after the switch",
        )

    async def _auth_recheck(self) -> None:
        self._record(
            "auth_recheck", await self._auth_finding(), "platform auth admin, product live"
        )

    async def _freeze(self) -> None:
        await self._release_session()
        self.store.conclude()

    # --- 7. cleanup -------------------------------------------------------------

    async def cleanup(self) -> str:
        """Tear down only this operation's project, observe it archived, keep the verdict."""
        project_id = self.ids.get("project_id")
        if project_id is None:
            self.store.cleanup(CleanupStatus.NOTHING_OWNED)
            return CleanupStatus.NOTHING_OWNED.value
        if self.store.record["cleanup"]["status"] == CleanupStatus.COMPLETED.value:
            return CleanupStatus.COMPLETED.value
        self.store.enter(Phase.TEARDOWN)
        self.store.cleanup(CleanupStatus.PENDING, project_id=project_id)
        started = self.clock.wall()
        try:
            state = await self.api.request_teardown(project_id, self.buyer)
            while state.get("status") == "pending":
                if (
                    self.clock.wall() - started
                ).total_seconds() >= self.config.deadlines.teardown_seconds:
                    self.store.cleanup(
                        CleanupStatus.FAILED,
                        project_id=project_id,
                        reason="teardown_timeout",
                        pending_application_ids=state.get("pending_application_ids"),
                    )
                    return CleanupStatus.FAILED.value
                await self.clock.sleep(self.config.deadlines.poll_seconds)
                state = await self.api.teardown_state(project_id, self.buyer)
            project = await self.api.project(project_id, as_user=self.buyer)
        except ApiRefused as error:
            self.store.cleanup(
                CleanupStatus.FAILED,
                project_id=project_id,
                reason="teardown_refused",
                route=error.route,
                status=error.status,
            )
            return CleanupStatus.FAILED.value
        facts = {
            "project_id": project_id,
            "teardown_status": state.get("status"),
            "project_status": project.get("status"),
            "released_bot_username": state.get("released_bot_username"),
        }
        if state.get("status") != "completed" or project.get("status") != "archived":
            self.store.cleanup(CleanupStatus.FAILED, reason="teardown_not_completed", **facts)
            return CleanupStatus.FAILED.value
        self.store.cleanup(CleanupStatus.COMPLETED, **facts)
        self.store.complete(Phase.TEARDOWN)
        return CleanupStatus.COMPLETED.value
