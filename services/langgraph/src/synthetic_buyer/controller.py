"""One fresh Telegram purchase, native acceptance and owned project cleanup.

The operator sequences one buyer operation and schedules no independent QA using
this account during buyer Telegram phases. Protected credentials stay with the
controller; the persona receives only customer context. Unknown sends are never
resent. Native work is observed with the buyer disconnected, and probes begin
only after correlated terminal deployment and typed QA passed. Evidence is frozen
before owner-authenticated teardown and deletion. Interrupted runs are inspectable.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
import hashlib
import re
from typing import Any

import httpx

from shared.contracts.dto.project import ProjectStatus, TeardownStatus
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
from .persona import Persona, PersonaContext, PersonaDecision, PersonaInvalid, deviation
from .platform_evidence import (
    PlatformFacts,
    ReaderUsageRefused,
    StoredSecrets,
    platform_product_id,
)
from .repository_evidence import RepositoryFacts, repository_name
from .telegram import Message, Peer, TelegramPort, TransportError

#: How often an open dialog is re-read while a reply is awaited.
DIALOG_POLL_SECONDS = 2
#: How much of the dialog the persona is shown.
PERSONA_TRANSCRIPT = 40
#: Reader usage reads, one poll apart, before product activity is called unknown.
USAGE_READS = 3
BOT_TOKEN = re.compile(r"\d{6,12}:[A-Za-z0-9_-]{30,}")
TOKEN_REQUEST = re.compile(r"токен|token", re.IGNORECASE)
#: The secret key the API stores a validated product bot token under.
TELEGRAM_TOKEN_KEY = "TELEGRAM_BOT_TOKEN"  # noqa: S105 - a key name
#: The project-config pointer the PO keeps at the open brief revision.
BRIEF_POINTER_KEY = "product_brief_id"
SHOWN = {
    "promo_code": "[покупатель отправил промокод]",
    "product_token": "[покупатель отправил токен бота]",
}
#: The controller's own words before the order's project is proven and admitted.
OPENING = "Здравствуйте! Хочу заказать нового Telegram-бота для себя. Токен бота у меня есть."
DEFERRAL = (
    "Подробно опишу, что должен делать бот, сразу после создания проекта. "
    "Создайте, пожалуйста, проект."
)
DESCRIBE = (
    "Проект создан. Опиши, что должен делать бот, отвечай на вопросы, выбирай русский язык, "
    "соглашайся на разбиение на этапы и подтверди описание заказа, если оно совпадает с "
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
    """Wall time for bounded phases, and the one way to wait."""

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


def digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


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
        repository: RepositoryFacts,
        stored_secrets: StoredSecrets,
        store: EvidenceStore,
        clock: Clock,
        environ: Mapping[str, str],
    ) -> None:
        self.config = config
        self.telegram = telegram
        self.api = api
        self.persona = persona
        self.platform = platform
        self.repository = repository
        self._stored_secrets = stored_secrets
        self.store = store
        self.clock = clock
        self.environ = environ
        self._token: str | None = None
        self._peers: dict[str, Peer] = {}
        self._using_telegram = False

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

    def _record(self, name: str, finding: native.Finding, provenance: str) -> None:
        self.store.observe(name, finding.status, provenance=provenance, detail=finding.detail)

    # --- the authority path: entrypoints ----------------------------------------

    def _phases(self) -> list[tuple[Phase, Callable[[], Awaitable[None]]]]:
        return [
            (Phase.PREFLIGHT, self._preflight),
            (Phase.REGISTRATION, self._registration),
            (Phase.PRODUCT_TOKEN, self._product_token_phase),
            (Phase.ORDER, self._order),
            (Phase.HANDOFF, self._handoff),
            (Phase.BUILD, self._build),
            (Phase.PRODUCT_PROBE, self._product_probe),
            (Phase.PLATFORM, self._platform),
            (Phase.LANGUAGE, self._language),
            (Phase.AUTH_RECHECK, self._auth_recheck),
            (Phase.FREEZE, self._freeze),
        ]

    async def run(self) -> Outcome:
        """Execute one fresh operation, freeze its verdict, then clean up its project."""
        if self.store.record.get("invoked_at"):
            return Outcome(self.store.record["verdict"]["status"], CleanupStatus.REFUSED.value)
        self.store.record["invoked_at"] = self.store.now()
        self.store.save()
        try:
            self._validate_identity()
            self._protect_inputs()
            await self._accept()
        except Stop as stop:
            self.store.fail(Phase.PREFLIGHT, stop.reason, self.store.redaction.value(stop.detail))
        except MissingSecret as error:
            self.store.fail(Phase.PREFLIGHT, "MissingSecret", str(error))
        return Outcome(self.store.record["verdict"]["status"], await self._settle_cleanup())

    async def _accept(self) -> None:
        phase = Phase(self.store.record["phase"])
        try:
            for phase, step in self._phases():
                self.store.enter(phase)
                await step()
                self.store.complete(phase)
        except Stop as stop:
            self.store.fail(phase, stop.reason, self.store.redaction.value(stop.detail))
        except (TransportError, ApiRefused, MissingSecret, PersonaInvalid) as error:
            self.store.fail(phase, type(error).__name__, self.store.redaction.text(str(error)))
        except Exception as error:  # noqa: BLE001 - the text may hold what it was handed
            self.store.fail(phase, "unexpected_error", {"type": type(error).__name__})

    def _validate_identity(self) -> None:
        record = self.store.record
        if record["operation_id"] != self.config.operation_id:
            raise Stop("operation_mismatch", {"retained": record["operation_id"]})
        retained = self.ids.get("buyer_telegram_id")
        if retained is not None and retained != self.buyer:
            raise Stop("buyer_mismatch", {"retained": retained, "configured": self.buyer})

    def _protect_inputs(self) -> None:
        """Protect configured inputs before any dialog or persona sees an echo."""
        self.store.redaction.add(
            *(
                resolve_secret(handle, self.environ)
                for handle in self.config.secret_handles().values()
            )
        )

    @property
    def _tg(self) -> TelegramPort:
        """The session, only inside a connected buyer phase."""
        if not self._using_telegram:
            raise RuntimeError("Telegram used outside a connected buyer phase")
        return self.telegram

    @asynccontextmanager
    async def _telegram(self, purpose: str) -> AsyncIterator[None]:
        """Connect and prove the buyer for a Telegram phase, disconnect on exit.

        The operator sequences this operation and independent same-account QA.
        This context proves the buyer's handoff, without a concurrency lock.
        """
        if self._using_telegram:
            yield
            return
        session = self._section("session")
        try:
            await self.telegram.connect()
            session["connected_at"] = self.store.now()
            me = await self.telegram.me()
            if me != self.buyer:
                raise Stop("identity_mismatch", {"expected": self.buyer, "actual": me})
            self.ids["buyer_telegram_id"] = me
            self._using_telegram = True
            yield
        finally:
            self._using_telegram = False
            try:
                await self.telegram.disconnect()
                session["disconnected_at"] = self.store.now()
            except TransportError as error:
                self.store.decide({"event": "disconnect_failed", "stage": error.stage})
                raise Stop("disconnect_failed", {"purpose": purpose}) from None
            finally:
                self.store.save()

    async def _peer(self, role: str, username: str, *, user_id: int | None = None) -> Peer:
        if role in self._peers:
            return self._peers[role]
        async with self._telegram(f"resolve:{role}"):
            peer = await self._tg.resolve(username)
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
        bot = self.config.codegen_bot
        return await self._peer("codegen", bot.username, user_id=bot.user_id)

    async def _botfather(self) -> Peer:
        return await self._peer("botfather", self.config.product_token.botfather_username or "")

    async def _product(self) -> Peer:
        return await self._peer("product", self.ids["product_bot_username"])

    async def _ensure_watermark(self, peer: Peer, dialog: str) -> None:
        marks = self.store.record["watermarks"]
        if dialog not in marks:
            async with self._telegram(f"watermark:{dialog}"):
                marks[dialog] = await self._tg.latest_id(peer)
            self.store.save()

    # --- reads and sends -------------------------------------------------------

    async def _read_new(self, peer: Peer, dialog: str) -> list[Message]:
        """Inbound messages from *peer* above the dialog's watermark, each read once."""
        marks = self.store.record["watermarks"]
        mark = marks[dialog]
        inbound = []
        async with self._telegram(f"read:{dialog}"):
            found = await self._tg.messages_after(peer, mark)
        for message in found:
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
        """What *peer* answers: every message until it has been quiet for the settle time.

        Waiting on a reply is Telegram use, so it is held as one.
        """
        settle = self.config.deadlines.settle_seconds
        async with self._telegram(f"reply:{dialog}"):
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
                if collected and (
                    (now - last_new).total_seconds() >= settle or waited >= 2 * timeout
                ):
                    return collected
                if not collected and waited >= timeout:
                    return []
                await self.clock.sleep(DIALOG_POLL_SECONDS)

    async def _find_delivery(self, peer: Peer, intent: dict) -> Message | None:
        """The outgoing message an intent named, looked for a bounded number of times."""
        async with self._telegram(f"delivery:{intent['dialog']}"):
            for check in range(self.config.deadlines.delivery_checks):
                if check:
                    await self.clock.sleep(DIALOG_POLL_SECONDS)
                try:
                    recent = await self._tg.messages_after(peer, intent["after"])
                except TransportError:
                    continue
                for message in recent:
                    if message.outgoing and digest(message.text) == intent["digest"]:
                        return message
        return None

    def _receipt(self, intent: dict, sent: Message) -> None:
        marks = self.store.record["watermarks"]
        self.store.message(
            intent["dialog"],
            {
                "direction": "out",
                "kind": intent["kind"],
                "id": sent.id,
                "date": sent.date.isoformat(),
                "text": intent["shown"],
            },
        )
        marks[intent["dialog"]] = max(marks.get(intent["dialog"], 0), sent.id)
        self.store.record["pending"] = None
        self.store.save()

    async def _send(
        self, peer: Peer, dialog: str, text: str, *, kind: str, shown: str | None = None
    ) -> Message:
        """Persist the intent, send once and record the receipt.

        A send whose receipt is lost is looked for in the dialog; one that cannot be
        found is an unknown delivery and stops the operation. It is never sent again.
        """
        if self.store.record.get("pending"):
            raise Stop("unresolved_action", {"kind": self.store.record["pending"]["kind"]})
        async with self._telegram(f"send:{dialog}"):
            intent = {
                "effect": "send",
                "dialog": dialog,
                "kind": kind,
                "digest": digest(text),
                "shown": shown if shown is not None else self.store.redaction.text(text),
                "after": self.store.record["watermarks"][dialog],
                "at": self.store.now(),
            }
            self.store.record["pending"] = intent
            self.store.save()
            try:
                sent = await self._tg.send(peer, text)
            except TransportError as error:
                self.store.decide(
                    {"event": "send_receipt_lost", "kind": kind, "stage": error.stage}
                )
                found = await self._find_delivery(peer, intent)
                if found is None:
                    raise Stop("delivery_unknown", {"dialog": dialog, "kind": kind}) from None
                sent = found
            self._receipt(intent, sent)
        return sent

    async def _exchange(
        self, peer: Peer, dialog: str, text: str, *, kind: str, timeout: float, **shown: str
    ) -> tuple[Message, list[Message]]:
        """One connected phase for a send and the answer to it."""
        async with self._telegram(f"exchange:{dialog}"):
            sent = await self._send(peer, dialog, text, kind=kind, **shown)
            return sent, await self._await_reply(peer, dialog, timeout=timeout)

    # --- 1. preflight -----------------------------------------------------------

    async def _preflight(self) -> None:
        await self._codegen()
        self.store.stamp("identity_proven_at")

    # --- 2. registration --------------------------------------------------------

    async def _promo(self) -> dict:
        """Mint one promo, keeping diagnostic intent before the request."""
        self.store.record["pending"] = {
            "effect": "mint",
            "kind": "promo_mint",
            "at": self.store.now(),
        }
        self.store.save()
        policy = self.config.registration
        promo = await self.api.mint_promo(
            credits_microusd=policy.credits_microusd,
            reservation_microusd=policy.attempt_reservation_microusd,
        )
        self._adopt_promo(promo)
        return promo

    def _adopt_promo(self, promo: dict) -> None:
        self.store.redaction.add(promo["code"])
        registration = self._section("registration")
        registration.setdefault("promo_code_ids", []).append(promo["id"])
        self.store.record["pending"] = None
        self.store.save()

    async def _registration(self) -> None:
        registration = self._section("registration")
        user = await self.api.user_by_telegram(self.buyer)
        if user is not None:
            self.ids["user_id"] = user["id"]
            registration["mode"] = "reused"
            return
        peer = await self._codegen()
        await self._ensure_watermark(peer, "codegen")
        # Mint the promo before connecting to Telegram.
        promo = await self._promo()
        async with self._telegram("registration"):
            if not self._sent("codegen", "promo_code"):
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
        self._section("product_token")["source"] = self._token_source()

    def _token_source(self) -> str:
        handle = self.config.product_token.handle
        if handle is not None:
            return handle.describe()
        return f"botfather:@{botfather_username(self.config.operation_id)}"

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
        _, replies = await self._exchange(
            peer, "botfather", text, kind="botfather", timeout=self.config.deadlines.reply_seconds
        )
        return replies

    @staticmethod
    def _token_in(replies: list[Message]) -> str | None:
        for message in replies:
            found = BOT_TOKEN.search(message.text)
            if found:
                return found.group(0)
        return None

    async def _botfather_token(self) -> str:
        """Create this operation's bot in one bounded BotFather dialog."""
        async with self._telegram("botfather"):
            return await self._botfather_dialog()

    async def _botfather_dialog(self) -> str:
        peer = await self._botfather()
        await self._ensure_watermark(peer, "botfather")
        state = self._section("botfather")
        username = botfather_username(self.config.operation_id)
        state["requested_username"] = username
        self.store.save()
        await self._botfather_exchange(peer, "/newbot")
        await self._botfather_exchange(peer, self.config.product_token.bot_display_name or "")
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

    def _sent(self, dialog: str, kind: str) -> int:
        """How many sends of *kind* this operation holds a receipt for in *dialog*."""
        return sum(
            1
            for entry in self.store.record["conversation"][dialog]
            if entry["direction"] == "out" and entry.get("kind") == kind
        )

    def _order_entries(self) -> list[dict]:
        order = self._section("order")
        return self.store.record["conversation"]["codegen"][order["conversation_from"] :]

    async def _order(self) -> None:
        peer = await self._codegen()
        await self._ensure_watermark(peer, "codegen")
        order = self._section("order")
        if "baseline_project_ids" not in order:
            owned = await self.api.owned_projects(self.buyer)
            order["baseline_project_ids"] = sorted(str(project["id"]) for project in owned)
            order["started_at"] = self.store.now()
            order["conversation_from"] = len(self.store.record["conversation"]["codegen"])
            self.store.save()
        latest = await self._read_new(peer, "codegen")
        # Telegram contexts close before API reads, persona turns and native work.
        entries = self._order_entries()
        expect_reply = not latest and bool(entries) and entries[-1]["direction"] == "out"
        while True:
            await self._observe_project()
            if await self._observe_story():
                self.store.stamp("order_accepted_at")
                return
            if self._elapsed_since(order["started_at"]) >= self.config.deadlines.order_seconds:
                raise Stop("order_timeout", self._order_stall_detail())
            if order.get("confirmation_sent_at"):
                await self.clock.sleep(self.config.deadlines.poll_seconds)
                continue
            if expect_reply:
                latest = await self._await_reply(
                    peer, "codegen", timeout=self.config.deadlines.reply_seconds
                )
                expect_reply = False
                if not latest:
                    await self._observe_project()
                    if await self._observe_story():
                        continue
                    raise Stop("conversation_stalled", self._order_stall_detail())
                continue
            await self._act(peer, latest)
            latest = []
            expect_reply = True

    async def _act(self, peer: Peer, latest: list[Message]) -> None:
        """Admit exactly the one action the order's current state allows, and take it."""
        order = self._section("order")
        if not any(entry["direction"] == "out" for entry in self._order_entries()):
            await self._send(peer, "codegen", OPENING, kind="order_opening")
            return
        asked_token = any(TOKEN_REQUEST.search(message.text) for message in latest)
        if asked_token and not self._sent("codegen", "product_token"):
            token = await self._product_token()
            order["token_sha256"] = digest(token)
            await self._send(
                peer, "codegen", token, kind="product_token", shown=SHOWN["product_token"]
            )
            return
        if not order.get("allowlist_applied_at"):
            if self._sent("codegen", "deferral") >= self.config.deadlines.deferrals:
                raise Stop("project_not_proven", self._order_stall_detail())
            await self._send(peer, "codegen", DEFERRAL, kind="deferral")
            return
        await self._persona_act(peer, latest)

    async def _persona_act(self, peer: Peer, latest: list[Message]) -> None:
        """The customer's words, sent only across a boundary the controller admitted."""
        order = self._section("order")
        turn = await self.persona.turn(self._persona_context(latest))
        self.store.decide({"event": "persona_turn", **turn.model_dump(mode="json")})
        reason = deviation(turn, self.config.scenario)
        if reason is not None:
            raise Stop(
                "persona_deviation",
                {"reason": reason, "text": self.store.redaction.text(turn.text or "")},
            )
        if turn.decision is PersonaDecision.IMPOSSIBLE:
            raise Stop("order_impossible", {"bot": probe.texts(latest)})
        if turn.decision is PersonaDecision.WAIT:
            return
        turns = order["turns"] = order.get("turns", 0) + 1
        if turns > self.config.deadlines.conversation_turns:
            raise Stop("turn_limit", {"turns": turns - 1})
        await self._admit_open_brief()
        if turn.decision is PersonaDecision.PRESS:
            await self._press(peer, latest, turn.button or "")
        else:
            await self._send(peer, "codegen", turn.text or "", kind="persona")
        if order.get("admitted_brief"):
            # This response may confirm the brief. From here native work can start;
            # observe its Story through the API, with no further Telegram use.
            order["confirmation_sent_at"] = self.store.now()
            self.store.save()

    async def _press(self, peer: Peer, latest: list[Message], label: str) -> None:
        for message in reversed(latest):
            for button in message.buttons:
                if button.text == label and button.data is not None:
                    if self.store.record.get("pending"):
                        raise Stop(
                            "unresolved_action", {"kind": self.store.record["pending"]["kind"]}
                        )
                    async with self._telegram("press"):
                        self.store.record["pending"] = {
                            "effect": "press",
                            "dialog": "codegen",
                            "kind": "press",
                            "message_id": message.id,
                            "at": self.store.now(),
                        }
                        self.store.save()
                        await self._tg.press(peer, message.id, button.data)
                    self.store.message(
                        "codegen",
                        {
                            "direction": "out",
                            "kind": "press",
                            "message_id": message.id,
                            "text": self.store.redaction.text(button.text),
                        },
                    )
                    self.store.record["pending"] = None
                    self.store.save()
                    return
        raise Stop("button_not_visible", {"button": label})

    async def _admit_open_brief(self) -> None:
        """While the project holds an open brief revision, prove its route before any reply.

        The PO points the project's config at the revision it presented
        (`product_brief_id`). Any reply could be read as its confirmation, so none
        is sent until that revision's stored capability plan routes a module with
        its install and its preview was made after the rollout readback.
        """
        project_id = self.ids["project_id"]
        order = self._section("order")
        project = await self.api.project(project_id)
        pointer = (project.get("config") or {}).get(BRIEF_POINTER_KEY)
        if not pointer:
            return
        brief = await self.api.brief(pointer)
        if str(brief.get("project_id")) != project_id:
            raise Stop("brief_of_another_project", {"brief_id": pointer})
        plan = await self.api.capability_plan(pointer)
        route = native.plan_routes(plan)
        if plan is None or route.status is not ObservationStatus.OBSERVED:
            raise Stop("route_not_module", {"brief_id": pointer, "plan": route.detail})
        preview = await self.api.capability_preview(plan.preview_id)
        ordering = native.preview_after_allowlist(
            preview, order.get("allowlist_applied_at"), project_id
        )
        if ordering.status is not ObservationStatus.OBSERVED:
            raise Stop("preview_not_after_allowlist", ordering.detail)
        order["admitted_brief"] = {
            "brief_id": pointer,
            "revision": brief.get("revision"),
            "preview_id": plan.preview_id,
            "routes": route.detail["capabilities"],
            "at": self.store.now(),
        }
        self.store.save()

    def _persona_context(self, latest: list[Message]) -> PersonaContext:
        transcript = [
            {"from": "bot" if entry["direction"] == "in" else "me", "text": entry.get("text", "")}
            for entry in self.store.record["conversation"]["codegen"][-PERSONA_TRANSCRIPT:]
        ]
        return PersonaContext(
            instruction=DESCRIBE,
            transcript=transcript,
            latest=[
                {
                    "text": self.store.redaction.text(message.text),
                    "buttons": [self.store.redaction.text(b.text) for b in message.buttons],
                }
                for message in latest
            ],
        )

    def _order_stall_detail(self) -> dict:
        order = self._section("order")
        return {
            "last": self._order_entries()[-4:],
            "token_source": self._token_source(),
            "token_sent": bool(self._sent("codegen", "product_token")),
            "unproven_candidates": order.get("unproven_candidates", []),
        }

    async def _observe_project(self) -> None:
        """Prove the order's project by the token it holds, then admit it to the rollout.

        Owner and timing only narrow the candidates. The proof is that the project's own
        encrypted secrets hold exactly the product token this operation sent in its
        Telegram order; a candidate without it is never adopted, admitted or torn down.
        """
        order = self._section("order")
        if "project_id" in self.ids:
            if not order.get("allowlist_applied_at"):
                await self._allowlist(self.ids["project_id"])
            return
        if not order.get("token_sha256") or not self._sent("codegen", "product_token"):
            return
        owned = await self.api.owned_projects(self.buyer)
        new = [str(p["id"]) for p in owned if str(p["id"]) not in order["baseline_project_ids"]]
        proven = []
        for project_id in new:
            stored = await self._stored_secrets(project_id)
            self.store.redaction.add(*(v for v in stored.values() if isinstance(v, str)))
            held = stored.get(TELEGRAM_TOKEN_KEY)
            if isinstance(held, str) and digest(held) == order["token_sha256"]:
                proven.append(project_id)
        order["unproven_candidates"] = sorted(set(new) - set(proven))
        if not proven:
            return
        if len(proven) > 1:
            raise Stop("ambiguous_new_projects", {"project_ids": sorted(proven)})
        project_id = proven[0]
        readback = await self.api.project(project_id, as_user=self.buyer)
        if readback.get("owner_id") != self.ids.get("user_id"):
            raise Stop("project_not_owned", {"project_id": project_id})
        created = native.parse_time(readback.get("created_at"))
        started = datetime.fromisoformat(order["started_at"])
        if created is None or created < started - timedelta(seconds=1):
            raise Stop("project_predates_order", {"project_id": project_id})
        if not readback.get("initiating_run_id"):
            raise Stop("project_without_initiating_run", {"project_id": project_id})
        self.store.record["ownership"] = {
            "project_id": project_id,
            "initiating_run_id": readback["initiating_run_id"],
            "token_sha256": order["token_sha256"],
            "token_message_id": next(
                (e["id"] for e in self._order_entries() if e.get("kind") == "product_token"),
                None,
            ),
            "proven_at": self.store.now(),
        }
        self.ids["project_id"] = project_id
        self.ids["initiating_run_id"] = readback["initiating_run_id"]
        self.store.stamp("project_proven_at")
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
        if not self._section("order").get("allowlist_applied_at"):
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
        """The buyer is disconnected before observing native work and QA."""
        if self._using_telegram or self.telegram.connected:
            raise RuntimeError("the buyer is still connected at the handoff")
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

    async def _fact(self, read: Awaitable[Any], what: str) -> Any:
        """One repository fact, or None (unknown) with the reason kept."""
        try:
            return await read
        except Exception as error:  # noqa: BLE001 - an unreadable fact stays unknown
            self.store.decide(
                {"event": "repository_fact_unavailable", "fact": what, "type": type(error).__name__}
            )
            return None

    async def _repository(self, project_id: str) -> str | None:
        names = [
            name
            for repo in await self.api.repositories(project_id)
            if (name := repository_name(str(repo.get("git_url") or "")))
        ]
        return names[0] if len(names) == 1 else None

    async def _observe_native(self, story: dict) -> None:  # noqa: C901, PLR0915 - one ordered read
        project_id, story_id = self.ids["project_id"], self.ids["story_id"]
        brief = await self.api.brief_by_story(story_id)
        self._record("brief_frozen", native.brief_frozen(brief), "GET /api/product-briefs/by-story")
        plan = await self.api.capability_plan(brief["id"]) if brief else None
        self._record("capability_plan_module", native.plan_routes(plan), "GET capability-plan")
        preview = await self.api.capability_preview(plan.preview_id) if plan else None
        self._record(
            "preview_after_allowlist",
            native.preview_after_allowlist(
                preview, self._section("order").get("allowlist_applied_at"), project_id
            ),
            "GET /api/capability-previews/{id} vs. rollout readback",
        )
        tasks = await self.api.tasks(story_id)
        engineering = await self.api.runs(story_id=story_id, run_type=RunType.ENGINEERING)
        self.ids["task_ids"] = [task["id"] for task in tasks]
        repository = await self._repository(project_id)
        self.ids["repository"] = repository
        timeline = story.get("generated_product_timeline") or {}
        number = story.get("pr_number") or (timeline.get("pull_request") or {}).get("number")
        pull_request = (
            await self._fact(self.repository.pull_request(repository, int(number)), "pull_request")
            if repository and number
            else None
        )
        operations = {
            task["id"]: task.get("install_operation") or {} for task in native.install_tasks(tasks)
        }
        published = {}
        for task_id, operation in operations.items():
            base, head = operation.get("base_sha"), operation.get("head_sha")
            published[task_id] = (
                await self._fact(self.repository.compare(repository, base, head), "install commits")
                if repository and base and head
                else None
            )
        bases = {operation.get("base_sha") for operation in operations.values()}
        last = [op.get("head_sha") for op in operations.values() if op.get("head_sha") not in bases]
        delta = (
            await self._fact(
                self.repository.compare(repository, last[0], pull_request.head_sha),
                "engineering delta",
            )
            if repository and pull_request and pull_request.head_sha and len(last) == 1 and last[0]
            else None
        )
        chain, _ = native.install_chain(tasks, engineering, pull_request, published, delta)
        self._record("install_after_scaffold", chain, "typed install operations + repository")
        self._record(
            "engineering_glue_only",
            native.glue_only(tasks, plan, delta),
            "repository change after the install vs. the kit's admitted glue",
        )
        self._record("product_ci", native.product_ci(story), "story generated_product_timeline")
        deploys = await self.api.runs(story_id=story_id, run_type=RunType.DEPLOY)
        typed, deploy = native.deploy_typed(project_id, story_id, deploys)
        if deploy is None:
            self._record("deploy_success", typed, "GET /api/runs type=deploy typed result")
        else:
            await self._observe_provenance(deploy, story, repository, pull_request)
        qa_runs = await self.api.runs(story_id=story_id, run_type=RunType.QA)
        qa = native.qa_passed(project_id, story_id, qa_runs, deploy)
        self._record("qa_passed", qa, "GET /api/runs type=qa typed result")
        bound = native.unknown("no successful deploy names a bot")
        if deploy is not None:
            self.ids.update(
                deploy_run_id=deploy["run_id"],
                application_id=deploy["application_id"],
                product_bot_username=deploy["bot_username"],
                deployed_url=deploy["deployed_url"],
            )
            liveness = await self.api.bot_liveness(project_id)
            alive = (
                liveness.get("state") == "alive"
                and str(liveness.get("bot_username", "")).casefold()
                == deploy["bot_username"].casefold()
            )
            bound = native.observed(liveness) if alive else native.failed(liveness)
        self._record("product_bot_bound", bound, "GET /api/projects/{id}/telegram/liveness")
        if isinstance(qa.detail, dict) and qa.detail.get("run_id"):
            self.ids["qa_run_id"] = qa.detail["run_id"]
        settled = [typed.status, qa.status, bound.status]
        if settled != [ObservationStatus.OBSERVED] * 3:
            raise Stop(
                "native_acceptance_not_settled", {"statuses": [item.value for item in settled]}
            )

    async def _observe_provenance(
        self,
        deploy: dict,
        story: dict,
        repository: str | None,
        pull_request: Any,
    ) -> None:
        """The deploy run, the commit's own publication and its build-and-push jobs."""
        workflow_id = deploy.get("deploy_workflow_run_id")
        commit = deploy.get("deployed_commit_sha")
        deployment_run = (
            await self._fact(self.repository.workflow_run(repository, workflow_id), "deploy run")
            if repository and isinstance(workflow_id, int)
            else None
        )
        publications = (
            await self._fact(self.repository.publication_runs(repository, commit), "publication")
            if repository and isinstance(commit, str) and commit
            else None
        )
        candidates = [
            run
            for run in publications or []
            if run.conclusion == "success" and run.head_sha == commit
        ]
        latest = max(candidates, key=lambda run: run.id, default=None)
        jobs = (
            await self._fact(
                self.repository.workflow_jobs(repository, latest.id), "publication jobs"
            )
            if repository and latest is not None
            else None
        )
        finding, publication = native.deploy_provenance(
            deploy, story, repository, pull_request, deployment_run, publications, jobs
        )
        if publication is not None:
            self.ids["publication_run_id"] = publication.id
        self.ids["deploy_workflow_run_id"] = workflow_id
        self._record(
            "deploy_success",
            finding,
            "typed deploy result + merged PR + ci.yml build-and-push publication + "
            "distinct deploy.yml run + sha-tagged image digests",
        )

    # --- 6. the live product -------------------------------------------------------

    async def _command(self, peer: Peer, command: str) -> tuple[Message, list[Message]]:
        return await self._exchange(
            peer,
            "product",
            command,
            kind="probe",
            timeout=self.config.deadlines.probe_reply_seconds,
        )

    async def _product_probe(self) -> None:
        """RU replies and `/channels`, then unsolicited delivery, then `/digest` on its own.

        The unsolicited post is waited for before `/digest` is sent: `/digest` is the
        released product's only command whose answer carries channel posts, and a
        multipart answer to it can arrive late, unquoted and linking a fresh post.
        """
        peer = await self._product()
        await self._ensure_watermark(peer, "product")
        channels = self.config.scenario.public_channels
        _, started = await self._command(peer, "/start")
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
        _, listed = await self._command(peer, "/channels")
        missing = probe.missing_channels(listed, channels)
        self._record(
            "channels_listed",
            native.failed({"missing": missing})
            if missing or not listed
            else native.observed(probe.texts(listed)),
            "product /channels",
        )
        await self._await_post(peer)
        sent, digest_replies = await self._command(peer, "/digest")
        self._record(
            "digest_answered",
            native.observed(
                {
                    "replies": len(digest_replies),
                    "urls": sorted({url for message in digest_replies for url in message.urls}),
                }
            )
            if digest_replies
            else native.failed("no reply to /digest"),
            "product /digest",
        )
        self._section("probe")["digest_sent_at"] = sent.date.isoformat()

    def _retained_inbound(self, dialog: str) -> list[Message]:
        """Every inbound message of *dialog* this operation kept, as read."""
        return [
            Message(
                id=entry["id"],
                sender_id=entry["sender_id"],
                outgoing=False,
                date=datetime.fromisoformat(entry["date"]),
                text=entry["text"],
                urls=tuple(entry.get("urls") or ()),
                reply_to=entry.get("reply_to"),
            )
            for entry in self.store.record["conversation"][dialog]
            if entry["direction"] == "in"
        ]

    def _source_commands(self) -> list[str]:
        """The buyer's product commands that can answer with channel posts, sent or pending.

        From this run's complete history, including any unresolved send.
        """
        sent = [
            entry["text"]
            for entry in self.store.record["conversation"]["product"]
            if entry["direction"] == "out" and entry.get("text") not in probe.NON_SOURCE_COMMANDS
        ]
        pending = self.store.record.get("pending") or {}
        if pending.get("dialog") == "product":
            sent.append(pending.get("shown") or "")
        return sent

    async def _await_post(self, peer: Peer) -> None:
        """A post the product delivers on its own, proven before any `/digest` is sent.

        Counted only while this operation's product history holds no command that
        can answer with posts, and only for a message in the released
        `tg-channels.post` event's own form for the product's language, unquoted,
        linking a post of the configured channel the form names, which that channel
        itself dates no later than the delivery. Anything else stays unattributed;
        unknown is the answer when nothing qualifies. Each Telegram read has a
        connected phase; the waits between reads are disconnected.
        """
        provenance = "unsolicited tg-channels.post event before any /digest; post dated by channel"
        prior = self._source_commands()
        if prior:
            self._record(
                "post_delivered",
                native.unknown(
                    {"reason": "a command that answers with posts precedes", "commands": prior}
                ),
                provenance,
            )
            return
        language = self.config.scenario.product_language
        channels = self.config.scenario.public_channels
        started = self.clock.wall()
        judged: set[int] = set()
        unattributed: list[dict] = []
        while True:
            await self._read_new(peer, "product")
            for message in self._retained_inbound("product"):
                if message.id in judged:
                    continue
                judged.add(message.id)
                found, reason = await self._unsolicited_post(message, language, channels)
                if found is not None:
                    self._record("post_delivered", native.observed(found), provenance)
                    return
                if reason is not None:
                    unattributed.append({"message_id": message.id, "reason": reason})
            if (
                self.clock.wall() - started
            ).total_seconds() >= self.config.deadlines.post_delivery_seconds:
                break
            await self.clock.sleep(self.config.deadlines.poll_seconds)
        self._record(
            "post_delivered",
            native.unknown(
                {
                    "waited_seconds": self.config.deadlines.post_delivery_seconds,
                    "unattributed": unattributed[-10:],
                }
            ),
            provenance,
        )

    async def _unsolicited_post(
        self, message: Message, language: str, channels: list[str]
    ) -> tuple[dict | None, str | None]:
        """The delivered post *message* proves, or why it proves none (None: not a post)."""
        links = probe.channel_post_links(message, channels)
        if not links:
            return None, None
        if message.reply_to is not None:
            return None, "a reply"
        named = probe.event_channel(message, language)
        if named is None:
            return None, "not the post event's form"
        for channel, post_id, url in links:
            if channel != named:
                continue
            async with self._telegram("post-source"):
                published = await self._tg.post_date(channel, post_id)
            if published is None or message.date < published:
                return None, "the channel does not date the post before its delivery"
            return {
                "message_id": message.id,
                "delivered_at": message.date.isoformat(),
                "channel": channel,
                "post_id": post_id,
                "source_published_at": published.isoformat(),
                "url": url,
                "form": f"tg-channels.post ({language})",
                "prior_source_commands": [],
            }, None
        return None, "links a post of another channel than it names"

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
        self._record(
            "auth_key_active", await self._auth_finding(), "platform auth admin product keys"
        )
        self._record("reader_usage", await self._usage_finding(), "reader GET /v1/usage")

    async def _usage_finding(self) -> native.Finding:
        """Activity the reader attributes to this product's own key, read a few times."""
        project_id = self.ids["project_id"]
        expected = platform_product_id(project_id)
        reads = []
        for attempt in range(USAGE_READS):
            if attempt:
                await self.clock.sleep(self.config.deadlines.poll_seconds)
            try:
                usage = await self.platform.usage(project_id, self.config.platform.reader_base_url)
            except ReaderUsageRefused as refusal:
                return native.unknown({"refused": self.store.redaction.text(str(refusal))})
            reads.append(
                {
                    "product_id": usage.product_id,
                    "channels": usage.channels,
                    "requests_this_minute": usage.requests_this_minute,
                    "resolves_today": usage.resolves_today,
                }
            )
            if usage.product_id != expected:
                return native.failed({"expected_product_id": expected, "reads": reads})
            if usage.channels >= 1 and usage.requests_this_minute + usage.resolves_today >= 1:
                return native.observed({"product_id": expected, "reads": reads})
        return native.unknown({"product_id": expected, "reads": reads})

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
        _, replies = await self._command(peer, "/channels")
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
        self.store.conclude()

    # --- 7. cleanup -------------------------------------------------------------

    async def _settle_cleanup(self) -> str:
        """Revalidate owned order evidence, then perform ordinary terminal cleanup."""
        ownership = self.store.record.get("ownership")
        project_id = None if ownership is None else ownership["project_id"]
        if self.store.record.get("pending"):
            return self._refuse_cleanup(project_id, "unresolved_action")
        if self.telegram.connected:
            return self._refuse_cleanup(project_id, "session_still_connected")
        if ownership is None:
            self.store.cleanup(
                CleanupStatus.NOTHING_OWNED,
                unproven_candidates=self._section("order").get("unproven_candidates", []),
            )
            return CleanupStatus.NOTHING_OWNED.value
        try:
            if not await self._still_owned(ownership):
                return self._refuse_cleanup(project_id, "ownership_unproven")
        except ApiRefused as error:
            return self._refuse_cleanup(project_id, f"ownership_unreadable:{error.status}")
        except Exception as error:  # noqa: BLE001 - unproven ownership refuses, whatever the cause
            return self._refuse_cleanup(project_id, f"ownership_unreadable:{type(error).__name__}")
        self.store.enter(Phase.TEARDOWN)
        self.store.cleanup(CleanupStatus.PENDING, project_id=project_id)
        teardown = self._section("teardown")
        teardown.update(
            status=CleanupStatus.PENDING.value, project_id=project_id, at=self.store.now()
        )
        self.store.save()
        started = self.clock.wall()
        try:
            state = await self.api.request_teardown(project_id, self.buyer)
            if state.get("status") in {
                TeardownStatus.PENDING.value,
                TeardownStatus.COMPLETED.value,
            }:
                state = await self.api.teardown_state(project_id, self.buyer)
            while state.get("status") == TeardownStatus.PENDING.value:
                if (
                    self.clock.wall() - started
                ).total_seconds() >= self.config.deadlines.teardown_seconds:
                    teardown.update(status=CleanupStatus.FAILED.value, reason="teardown_timeout")
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
            teardown.update(
                status=CleanupStatus.FAILED.value, route=error.route, http_status=error.status
            )
            self.store.cleanup(
                CleanupStatus.FAILED,
                project_id=project_id,
                reason="teardown_refused",
                route=error.route,
                http_status=error.status,
            )
            return CleanupStatus.FAILED.value
        except Exception as error:  # noqa: BLE001 - an unreadable teardown never authorizes DELETE
            teardown.update(status=CleanupStatus.FAILED.value, error_type=type(error).__name__)
            self.store.cleanup(
                CleanupStatus.FAILED, project_id=project_id, reason="teardown_unknown"
            )
            return CleanupStatus.FAILED.value
        facts = {
            "project_id": project_id,
            "teardown_status": state.get("status"),
            "project_status": project.get("status"),
            "released_bot_username": state.get("released_bot_username"),
        }
        if (
            state.get("status") != TeardownStatus.COMPLETED.value
            or project.get("status") != ProjectStatus.ARCHIVED.value
        ):
            teardown.update(status=CleanupStatus.FAILED.value, **facts)
            self.store.cleanup(CleanupStatus.FAILED, reason="teardown_not_completed", **facts)
            return CleanupStatus.FAILED.value
        teardown.update(status=CleanupStatus.COMPLETED.value, **facts)
        self.store.complete(Phase.TEARDOWN)
        return await self._delete_owned(project_id, project, facts)

    async def _delete_owned(self, project_id: str, project: dict, facts: dict) -> str:
        """DELETE only after completed archived teardown; require owner GET 404."""
        ownership = self.store.record["ownership"]
        if (
            project.get("owner_id") != self.ids["user_id"]
            or project.get("initiating_run_id") != ownership["initiating_run_id"]
        ):
            return self._refuse_cleanup(project_id, "ownership_unproven")
        self.store.enter(Phase.DELETE)
        deletion = self._section("deletion")
        deletion.update(
            status=CleanupStatus.PENDING.value, project_id=project_id, at=self.store.now()
        )
        self.store.save()
        try:
            deletion["delete_status"] = await self.api.delete_project(project_id, self.buyer)
            self.store.save()
            if deletion["delete_status"] != httpx.codes.NO_CONTENT:
                raise Stop("delete_not_204")
            if not await self.api.deletion_confirmed(project_id, self.buyer):
                deletion["get_status"] = 200
                raise Stop("project_still_visible")
            deletion["get_status"] = 404
        except Exception as error:  # noqa: BLE001 - uncertain deletion remains failed, without retries
            deletion["status"] = CleanupStatus.FAILED.value
            if isinstance(error, ApiRefused):
                deletion.update(route=error.route, http_status=error.status)
            elif isinstance(error, Stop):
                deletion["reason"] = error.reason
            else:
                deletion["error_type"] = type(error).__name__
            self.store.cleanup(CleanupStatus.FAILED, reason="deletion_unconfirmed", **facts)
            return CleanupStatus.FAILED.value
        deletion["status"] = CleanupStatus.COMPLETED.value
        self.store.complete(Phase.DELETE)
        self.store.cleanup(CleanupStatus.COMPLETED, **facts)
        return CleanupStatus.COMPLETED.value

    async def _still_owned(self, ownership: dict) -> bool:
        """The retained proof still holds: same initiating run, same token in its secrets."""
        project = await self.api.project(ownership["project_id"], as_user=self.buyer)
        if project.get("owner_id") != self.ids["user_id"]:
            return False
        if project.get("initiating_run_id") != ownership["initiating_run_id"]:
            return False
        stored = await self._stored_secrets(ownership["project_id"])
        self.store.redaction.add(*(v for v in stored.values() if isinstance(v, str)))
        held = stored.get(TELEGRAM_TOKEN_KEY)
        return isinstance(held, str) and digest(held) == ownership["token_sha256"]

    def _refuse_cleanup(self, project_id: str | None, reason: str, **facts: Any) -> str:
        self.store.cleanup(CleanupStatus.REFUSED, project_id=project_id, reason=reason, **facts)
        return CleanupStatus.REFUSED.value
