"""The synthetic buyer's controller, end to end, over an in-process world.

Each case drives the real controller through injected Telegram, API, persona,
platform and clock ports and judges it by what it sent, what it changed through
the API and what its retained evidence says.
"""

from __future__ import annotations

import json

import pytest

from src.synthetic_buyer.controller import botfather_username
from src.synthetic_buyer.evidence import EvidenceStore, Redaction
from src.synthetic_buyer.persona import PersonaDecision, PersonaTurn, StatedRoute
from src.synthetic_buyer.platform_evidence import AuthFacts, UsageFacts
from tests.unit.synthetic_buyer.fakes import (
    API_HASH,
    BUYER,
    CODEGEN,
    INTERNAL_KEY,
    PRODUCT,
    PROJECT,
    PROMO,
    SESSION,
    STRANGER,
    TOKEN,
    UNRELATED_PROJECT,
    USER_ID,
    ProcessDied,
    harness,
)


def _codegen_sends(h) -> list[str]:
    return [
        event[2]
        for event in h.telegram.events
        if event[0] == "send" and event[1] == CODEGEN.username
    ]


def _verdict(h) -> dict:
    return h.store.record["verdict"]


def _observation(h, name) -> str:
    return h.store.record["observations"][name]["status"]


async def test_a_full_order_is_observed_end_to_end_and_its_project_torn_down(tmp_path):
    h = harness(tmp_path)

    outcome = await h.buyer().run()

    assert (outcome.verdict, outcome.cleanup, outcome.exit_code) == ("passed", "completed", 0), (
        h.store.record["verdict"]
    )
    assert _codegen_sends(h)[0] == PROMO
    assert TOKEN in _codegen_sends(h)
    ids = h.store.record["ids"]
    assert (ids["project_id"], ids["story_id"], ids["user_id"]) == (PROJECT, "story-0001", USER_ID)
    assert (ids["deploy_run_id"], ids["qa_run_id"]) == ("dep-1", "qa-1")
    assert ids["product_bot_username"] == PRODUCT.username
    deploy = h.store.record["observations"]["deploy_success"]["detail"]
    assert deploy["deployed_commit_sha"] == "b" * 40
    assert deploy["image_references"] == {"backend": "registry/p@sha256:" + "c" * 64}
    assert h.store.record["registration"]["mode"] == "redeemed"
    post = h.store.record["observations"]["post_delivered"]["detail"]
    assert (post["channel"], post["post_id"]) == ("chan_two", 201)
    assert h.world.projects[PROJECT]["status"] == "archived"
    assert h.store.record["cleanup"]["released_bot_username"] == PRODUCT.username


async def test_the_project_is_admitted_to_the_rollout_before_its_feature_is_previewed(tmp_path):
    h = harness(tmp_path)

    await h.buyer().run()

    (write,) = h.world.rollout_writes
    assert write["value"] == {"project_ids": [UNRELATED_PROJECT, PROJECT], "note": "operator-owned"}
    assert (
        h.world.previews
        and write["at"].isoformat() < h.world.previews["preview-" + "1" * 24]["created_at"]
    )
    assert _observation(h, "preview_after_allowlist") == "observed"


async def test_the_shared_session_is_released_while_native_work_and_qa_run(tmp_path):
    h = harness(tmp_path)

    await h.buyer().run()

    events = h.world.events
    first_story_read = events.index(("api", "story"))
    last_disconnect_before = max(
        index for index, event in enumerate(events[:first_story_read]) if event == ("disconnect",)
    )
    assert ("connect",) not in events[last_disconnect_before:first_story_read]
    reconnect = events.index(("connect",), first_story_read)
    assert ("api", "runs") in events[first_story_read:reconnect]
    assert not h.telegram.connected


async def test_nothing_is_sent_when_the_session_is_not_the_configured_buyer(tmp_path):
    h = harness(tmp_path, me=BUYER + 1)

    outcome = await h.buyer().run()

    assert [event for event in h.telegram.events if event[0] == "send"] == []
    assert (_verdict(h)["failure_phase"], _verdict(h)["reason"]) == (
        "preflight",
        "identity_mismatch",
    )
    assert outcome.cleanup == "nothing_owned"


async def test_stale_unrelated_and_duplicate_messages_never_reach_the_persona(tmp_path):
    h = harness(tmp_path)
    h.world.append(CODEGEN, "Старое сообщение до заказа")
    h.world.users[BUYER] = {"id": USER_ID}
    h.telegram.duplicate_reads = True
    h.world.append(CODEGEN, "Чужое сообщение", sender=STRANGER)
    await h.buyer().run()

    seen = [item["text"] for context in h.persona.contexts for item in context.latest]
    assert "Старое сообщение до заказа" not in seen
    assert "Чужое сообщение" not in seen
    assert len(seen) == len(set(seen))
    assert _codegen_sends(h).count("Здравствуйте! Хочу заказать нового Telegram-бота.") == 1
    assert h.store.record["registration"]["mode"] == "reused"
    assert "mint_promo" not in h.api.calls


async def test_a_from_scratch_route_stops_before_the_brief_is_confirmed(tmp_path):
    h = harness(tmp_path)
    h.world.route = "from_scratch"

    outcome = await h.buyer().run()

    assert _verdict(h)["reason"] == "route_not_module"
    assert "Да, всё верно." not in _codegen_sends(h)
    assert h.world.stories == []
    assert outcome.cleanup == "completed"


async def test_a_confirmation_without_a_stated_module_route_is_never_sent(tmp_path):
    h = harness(tmp_path)
    h.persona.overrides["готовым решением"] = PersonaTurn(
        decision=PersonaDecision.REPLY, text="Да, подтверждаю.", confirms_brief=True
    )

    await h.buyer().run()

    assert _verdict(h)["reason"] == "confirmation_not_admitted"
    assert "Да, подтверждаю." not in _codegen_sends(h)


async def test_a_persona_giving_implementation_instructions_is_stopped(tmp_path):
    h = harness(tmp_path)
    h.persona.overrides["Что должен делать бот"] = PersonaTurn(
        decision=PersonaDecision.REPLY, text="Поставьте модуль tg-channels версии 0.1.2."
    )

    await h.buyer().run()

    assert (_verdict(h)["reason"], _verdict(h)["detail"]["reason"]) == (
        "persona_deviation",
        "implementation_instruction",
    )
    assert not any("модуль" in text for text in _codegen_sends(h))


async def test_secrets_reach_codegen_but_never_the_persona_or_the_evidence(tmp_path):
    h = harness(tmp_path)

    await h.buyer().run()

    shown = json.dumps([vars(context) for context in h.persona.contexts], ensure_ascii=False)
    retained = h.evidence_text()
    for secret in (TOKEN, PROMO, SESSION, API_HASH, INTERNAL_KEY, "install-op-secret-token"):
        assert secret not in shown
        assert secret not in retained
    assert "[покупатель отправил токен бота]" in shown
    assert h.store.record["product_token"]["source"] == "env:BUYER_PRODUCT_BOT_TOKEN"


async def test_a_token_asked_for_again_is_a_rejection_named_by_its_handle(tmp_path):
    h = harness(tmp_path)
    h.persona.overrides["Что должен делать бот"] = PersonaTurn(
        decision=PersonaDecision.WAIT, bot_asks_for_token=True
    )

    await h.buyer().run()

    verdict = _verdict(h)
    assert verdict["reason"] == "product_token_rejected"
    assert verdict["detail"]["source"] == "env:BUYER_PRODUCT_BOT_TOKEN"
    assert TOKEN not in h.evidence_text()
    assert _codegen_sends(h).count(TOKEN) == 1


async def test_a_story_that_never_settles_is_a_bounded_build_failure(tmp_path):
    h = harness(tmp_path)
    h.world.build_ticks = 10**6

    outcome = await h.buyer().run()

    assert (_verdict(h)["failure_phase"], _verdict(h)["reason"]) == ("build", "build_timeout")
    assert outcome.cleanup == "completed"


async def test_a_story_parked_for_a_person_stops_the_operation(tmp_path):
    h = harness(tmp_path)
    h.world.story_final = "waiting_human_review"

    await h.buyer().run()

    assert _verdict(h)["reason"] == "story_stopped"
    assert _verdict(h)["detail"]["status"] == "waiting_human_review"


async def test_a_deploy_of_another_project_is_not_this_orders_deploy(tmp_path):
    h = harness(tmp_path)
    h.world.deploy_project = UNRELATED_PROJECT

    await h.buyer().run()

    assert _verdict(h)["reason"] == "native_acceptance_not_settled"
    assert _observation(h, "deploy_success") == "failed"
    assert [e for e in h.telegram.events if e[0] == "send" and e[1] == PRODUCT.username] == []


async def test_a_new_project_owned_by_someone_else_is_never_adopted(tmp_path):
    h = harness(tmp_path)
    h.world.new_project_owner = 99

    outcome = await h.buyer().run()

    assert _verdict(h)["reason"] == "project_not_owned"
    assert "write_module_rollout" not in h.api.calls
    assert outcome.cleanup == "nothing_owned"


async def test_the_product_is_not_probed_while_a_qa_run_holds_the_account(tmp_path):
    h = harness(tmp_path)
    busy = {"id": "qa-other", "status": "running"}

    def qa_starts_when_story_completes():
        if h.world.stories and h.world.stories[0]["status"] == "completed" and not h.world.busy_qa:
            h.world.busy_qa.append(busy)
            h.clock.hooks.remove(qa_starts_when_story_completes)
            h.clock.hooks.append(qa_ends)

    def qa_ends():
        h.world.busy_qa.clear()
        h.clock.hooks.remove(qa_ends)

    h.clock.hooks.append(qa_starts_when_story_completes)

    outcome = await h.buyer().run()

    assert outcome.verdict == "passed"
    assert h.world.sends_while_qa_busy == []
    assert h.world.connects_while_qa_busy == 0
    assert "qa_quiet_at" in h.store.record["timestamps"]


async def test_a_qa_run_starting_during_the_post_wait_finds_the_session_released(tmp_path):
    h = harness(tmp_path)
    window = {}

    def qa_comes_and_goes():
        digest = getattr(h.world, "digest_at", None)
        if digest is None:
            return
        elapsed = (h.clock.now - digest).total_seconds()
        if 30 <= elapsed < 90 and not window:
            window["started"] = h.clock.now
            h.world.busy_qa.append({"id": "qa-other", "status": "running"})
        elif elapsed >= 90 and h.world.busy_qa:
            h.world.busy_qa.clear()

    h.clock.hooks.append(qa_comes_and_goes)

    outcome = await h.buyer().run()

    assert window and outcome.verdict == "passed", _verdict(h)
    assert h.world.connects_while_qa_busy == 0
    assert h.world.sends_while_qa_busy == []


async def test_a_qa_run_that_never_ends_is_a_bounded_failure_without_probes(tmp_path):
    h = harness(tmp_path)
    h.world.busy_qa.append({"id": "qa-stuck", "status": "queued"})

    await h.buyer().run()

    assert (_verdict(h)["reason"], _verdict(h)["detail"]) == (
        "qa_never_quiet",
        {"qa_run_ids": ["qa-stuck"]},
    )
    assert h.world.sends_while_qa_busy == []


@pytest.mark.parametrize(
    ("change", "observation"),
    [
        ("no_post", "post_delivered"),
        ("auth_revoked", "auth_key_active"),
        ("no_reader_use", "reader_usage"),
        ("no_language", "language_switched"),
    ],
)
async def test_a_missing_or_contradicted_live_fact_never_passes(tmp_path, change, observation):
    h = harness(tmp_path)
    if change == "no_post":
        h.world.post_after_digest = False
    elif change == "auth_revoked":
        h.world.auth = AuthFacts("orch-x", "abcdefghijk2", (), ("abcdefghijk2",))
    elif change == "no_reader_use":
        h.world.usage = UsageFacts(200, 0, 0, 0)
    else:
        h.world.language_switch_works = False

    outcome = await h.buyer().run()

    assert outcome.verdict in {"failed", "incomplete"}
    assert outcome.exit_code == 1
    assert _observation(h, observation) != "observed"
    assert outcome.cleanup == "completed"


async def test_an_interrupted_order_resumes_without_a_second_order(tmp_path):
    h = harness(tmp_path)
    h.persona.interrupt_on = "готовым решением"

    with pytest.raises(ProcessDied):
        await h.buyer().run()

    resumed_store = EvidenceStore(tmp_path, Redaction(), clock=h.clock.wall)
    resumed_store.load()
    h.store = resumed_store
    h.persona.interrupt_on = None
    h.telegram._connected = False  # the process died with its session

    outcome = await h.buyer().run()

    assert outcome.verdict == "passed", _verdict(h)
    sends = _codegen_sends(h)
    assert sends.count(PROMO) == 1
    assert sends.count(TOKEN) == 1
    assert sends.count("Здравствуйте! Хочу заказать нового Telegram-бота.") == 1
    assert len(h.world.rollout_writes) == 1
    assert [p for p in h.world.projects if p != UNRELATED_PROJECT] == [PROJECT]


async def test_a_failed_teardown_stays_visible_beside_the_verdict(tmp_path):
    h = harness(tmp_path)
    h.world.teardown = "failed"

    outcome = await h.buyer().run()

    assert (outcome.verdict, outcome.cleanup, outcome.exit_code) == ("passed", "failed", 1)
    assert h.store.record["cleanup"]["reason"] == "teardown_not_completed"


async def test_a_failure_stays_a_failure_whatever_the_cleanup_does(tmp_path):
    h = harness(tmp_path)
    h.world.route = "from_scratch"

    outcome = await h.buyer().run()

    assert (outcome.verdict, outcome.cleanup) == ("failed", "completed")
    again = await h.buyer().run()
    assert (again.verdict, again.cleanup) == ("failed", "completed")
    assert h.api.calls.count("request_teardown") == 1


async def test_a_send_whose_delivery_is_unknown_is_not_sent_twice(tmp_path):
    h = harness(tmp_path)
    h.world.users[BUYER] = {"id": USER_ID}
    h.telegram.fail_next_sends = 1

    await h.buyer().run()

    assert _codegen_sends(h).count("Здравствуйте! Хочу заказать нового Telegram-бота.") == 1
    first = h.store.record["conversation"]["codegen"][0]
    assert first["send_failures"] == ["send"]


async def test_an_unanswered_order_stalls_with_its_last_turns(tmp_path):
    h = harness(tmp_path)
    h.world.users[BUYER] = {"id": USER_ID}

    def bot_silent(text):
        return []

    h.world.codegen_answer = bot_silent

    await h.buyer().run()

    verdict = _verdict(h)
    assert (verdict["failure_phase"], verdict["reason"]) == ("order", "conversation_stalled")
    assert verdict["detail"]["last"][-1]["direction"] == "out"


async def test_the_route_named_by_the_bot_is_recorded_for_the_operator(tmp_path):
    h = harness(tmp_path)

    await h.buyer().run()

    assert h.store.record["order"]["stated_route"] == StatedRoute.MODULE.value
    assert (
        h.store.record["order"]["confirmed_at"] >= h.store.record["order"]["allowlist_applied_at"]
    )


BOTFATHER_MODE = {
    "mode": "botfather",
    "botfather_username": "BotFather",
    "bot_display_name": "Каналы покупателя",
}


def _botfather_sends(h) -> list[str]:
    return [e[2] for e in h.telegram.events if e[0] == "send" and e[1] == "BotFather"]


async def test_a_bot_created_in_botfather_is_owned_by_this_operation(tmp_path):
    h = harness(tmp_path)

    outcome = await h.buyer(product_token=BOTFATHER_MODE).run()

    assert outcome.verdict == "passed", _verdict(h)
    username = botfather_username("s1487-buyer-001")
    assert _botfather_sends(h) == ["/newbot", "Каналы покупателя", username]
    assert h.store.record["ids"]["botfather_bot_username"] == username
    assert TOKEN in _codegen_sends(h)
    assert TOKEN not in h.evidence_text()


async def test_an_interrupted_creation_reads_the_token_back_instead_of_a_second_bot(tmp_path):
    h = harness(tmp_path)
    h.world.botfather_dies_after_creation = True

    with pytest.raises(ProcessDied):
        await h.buyer(product_token=BOTFATHER_MODE).run()
    h.store = EvidenceStore(tmp_path, Redaction(), clock=h.clock.wall)
    h.store.load()
    h.telegram._connected = False

    outcome = await h.buyer(product_token=BOTFATHER_MODE).run()

    assert outcome.verdict == "passed", _verdict(h)
    assert _botfather_sends(h).count("/newbot") == 1
    assert len(h.world.botfather_bots) == 1
    assert "/token" in _botfather_sends(h)


async def test_a_bot_that_is_not_the_configured_codegen_bot_gets_nothing(tmp_path):
    h = harness(tmp_path)

    await h.buyer(codegen_bot={"username": CODEGEN.username, "user_id": CODEGEN.id + 1}).run()

    assert _verdict(h)["reason"] == "codegen_bot_mismatch"
    assert [event for event in h.telegram.events if event[0] == "send"] == []


async def test_a_promo_code_the_bot_does_not_redeem_is_a_registration_refusal(tmp_path):
    h = harness(tmp_path)
    h.world.codegen_answer = lambda text: ["Промокод недействителен."]

    outcome = await h.buyer().run()

    verdict = _verdict(h)
    assert (verdict["failure_phase"], verdict["reason"]) == ("registration", "registration_refused")
    assert verdict["detail"]["replies"] == ["Промокод недействителен."]
    assert h.store.record["registration"]["promo_code_ids"] == [1]
    assert PROMO not in h.evidence_text()
    assert outcome.cleanup == "nothing_owned"
