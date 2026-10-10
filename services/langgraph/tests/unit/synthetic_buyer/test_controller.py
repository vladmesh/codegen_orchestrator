"""The synthetic buyer's controller, end to end, over an in-process world.

Each case drives the real controller and its judgments through injected Telegram,
API, repository, persona, platform and clock ports, and judges it by what it sent,
what it changed through the API and what its retained evidence says.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
import json

import pytest

from src.synthetic_buyer.controller import DEFERRAL, OPENING, botfather_username
from src.synthetic_buyer.persona import PersonaDecision, PersonaTurn
from src.synthetic_buyer.platform_evidence import AuthFacts, UsageFacts, platform_product_id
from tests.unit.synthetic_buyer.fakes import (
    API_HASH,
    BRIEF,
    BUYER,
    CODEGEN,
    DIGEST,
    INTERNAL_KEY,
    MERGE,
    PREVIEW,
    PRODUCT,
    PROJECT,
    PROMO,
    SESSION,
    SIDE_PROJECT,
    START,
    STRANGER,
    TOKEN,
    UNRELATED_PROJECT,
    USER_ID,
    ProcessDied,
    harness,
    install_operation,
)


def _codegen_sends(h) -> list[str]:
    return [e[2] for e in h.telegram.events if e[0] == "send" and e[1] == CODEGEN.username]


def _verdict(h) -> dict:
    return h.store.record["verdict"]


def _observation(h, name) -> str:
    return h.store.record["observations"][name]["status"]


def _decisions(h) -> list[dict]:
    return h.store.record["conversation"]["decisions"]


def _po_that_creates_nothing(world):
    """A PO that redeems the code but never binds the order's token to a project."""

    def answer(text):
        if text == PROMO:
            return type(world).codegen_answer(world, text)
        if text == TOKEN:
            return ["Токен не подошёл. Попробуйте другой токен позже."]
        return ["Что должен делать бот? Расскажите подробнее."]

    return answer


def _wrap_product(h, after):
    """Keep the product's answers and call *after* with each command."""

    def answer(text):
        replies = type(h.world).product_answer(h.world, text)
        after(text)
        return replies

    h.world.product_answer = answer


# --- the whole route -------------------------------------------------------------


async def test_a_full_order_is_observed_end_to_end_and_its_project_torn_down(tmp_path):
    h = harness(tmp_path)

    outcome = await h.buyer().run()

    assert (outcome.verdict, outcome.cleanup, outcome.exit_code) == ("passed", "completed", 0), (
        _verdict(h)
    )
    assert _codegen_sends(h)[:3] == [PROMO, OPENING, TOKEN]
    ids = h.store.record["ids"]
    assert (ids["project_id"], ids["story_id"], ids["user_id"]) == (PROJECT, "story-0001", USER_ID)
    assert (ids["deploy_run_id"], ids["qa_run_id"]) == ("dep-1", "qa-1")
    assert ids["initiating_run_id"] == "po-0123456789ab"
    deploy = h.store.record["observations"]["deploy_success"]["detail"]
    assert deploy["deployed_commit_sha"] == MERGE
    assert deploy["image_digests"] == {"BACKEND_IMAGE": DIGEST}
    assert deploy["publication"]["conclusion"] == "success"
    post = h.store.record["observations"]["post_delivered"]["detail"]
    assert (post["channel"], post["post_id"]) == ("chan_two", 201)
    assert h.world.projects[PROJECT]["status"] == "archived"
    assert h.store.record["cleanup"]["released_bot_username"] == PRODUCT.username


async def test_the_project_is_admitted_to_the_rollout_before_its_feature_is_previewed(tmp_path):
    h = harness(tmp_path)

    await h.buyer().run()

    (write,) = h.world.rollout_writes
    assert write["value"] == {"project_ids": [UNRELATED_PROJECT, PROJECT], "note": "operator-owned"}
    assert write["at"].isoformat() < h.world.previews[PREVIEW]["created_at"]
    assert _observation(h, "preview_after_allowlist") == "observed"
    assert h.store.record["order"]["admitted_brief"]["routes"][0]["route"] == "module"


async def test_before_admission_only_the_controller_speaks(tmp_path):
    h = harness(tmp_path)

    await h.buyer().run()

    admitted_at = h.store.record["order"]["allowlist_applied_at"]
    kinds_before = {
        entry["kind"]
        for entry in h.store.record["conversation"]["codegen"]
        if entry["direction"] == "out" and entry["date"] <= admitted_at
    }
    assert kinds_before <= {"promo_code", "order_opening", "product_token", "deferral"}
    assert h.persona.contexts and all(
        "[покупатель отправил токен бота]" in json.dumps(c.transcript, ensure_ascii=False)
        for c in h.persona.contexts
    )


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


# --- identity, dialog hygiene ---------------------------------------------------------


async def test_nothing_is_sent_when_the_session_is_not_the_configured_buyer(tmp_path):
    h = harness(tmp_path, me=BUYER + 1)

    outcome = await h.buyer().run()

    assert [event for event in h.telegram.events if event[0] == "send"] == []
    assert (_verdict(h)["failure_phase"], _verdict(h)["reason"]) == (
        "preflight",
        "identity_mismatch",
    )
    assert outcome.cleanup == "nothing_owned"


async def test_a_bot_that_is_not_the_configured_codegen_bot_gets_nothing(tmp_path):
    h = harness(tmp_path)

    await h.buyer(codegen_bot={"username": CODEGEN.username, "user_id": CODEGEN.id + 1}).run()

    assert _verdict(h)["reason"] == "codegen_bot_mismatch"
    assert [event for event in h.telegram.events if event[0] == "send"] == []


async def test_stale_unrelated_and_duplicate_messages_never_reach_the_persona(tmp_path):
    h = harness(tmp_path)
    h.world.append(CODEGEN, "Старое сообщение до заказа")
    h.world.append(CODEGEN, "Чужое сообщение", sender=STRANGER)
    h.world.users[BUYER] = {"id": USER_ID}
    h.telegram.duplicate_reads = True

    await h.buyer().run()

    seen = [item["text"] for context in h.persona.contexts for item in context.latest]
    assert "Старое сообщение до заказа" not in seen
    assert "Чужое сообщение" not in seen
    assert len(seen) == len(set(seen))
    assert _codegen_sends(h).count(OPENING) == 1
    assert h.store.record["registration"]["mode"] == "reused"
    assert "mint_promo" not in h.api.calls


# --- BLOCKER-model-confirmation-gate and route admission -------------------------------


async def test_an_affirmative_model_reply_never_confirms_a_from_scratch_brief(tmp_path):
    h = harness(tmp_path)
    h.world.route = "from_scratch"
    h.persona.overrides = {"": PersonaTurn(decision=PersonaDecision.REPLY, text="Да, подтверждаю.")}

    outcome = await h.buyer().run()

    assert _verdict(h)["reason"] == "route_not_module"
    # The first affirmative went out before any brief existed; once one is open, none does.
    assert _codegen_sends(h).count("Да, подтверждаю.") == 2
    assert h.world.briefs[BRIEF]["confirmed_at"] is None
    assert h.world.stories == []
    assert outcome.cleanup == "completed"


async def test_no_model_reply_is_asked_for_before_the_project_is_admitted(tmp_path):
    h = harness(tmp_path)
    h.world.codegen_answer = _po_that_creates_nothing(h.world)
    h.persona.overrides = {"": PersonaTurn(decision=PersonaDecision.REPLY, text="Да.")}

    await h.buyer().run()

    assert h.persona.contexts == []
    assert "Да." not in _codegen_sends(h)
    assert _codegen_sends(h).count(DEFERRAL) == 3
    assert _verdict(h)["reason"] == "project_not_proven"


async def test_a_preview_made_before_the_rollout_refuses_confirmation(tmp_path):
    h = harness(tmp_path)

    def backdate_the_preview(text):
        replies = type(h.world).codegen_answer(h.world, text)
        if h.world.stage == "brief" and PREVIEW in h.world.previews:
            h.world.previews[PREVIEW]["created_at"] = START.isoformat()
        return replies

    h.world.codegen_answer = backdate_the_preview

    await h.buyer().run()

    assert _verdict(h)["reason"] == "preview_not_after_allowlist"
    assert "Да, всё верно." not in _codegen_sends(h)


async def test_a_persona_giving_implementation_instructions_is_stopped(tmp_path):
    h = harness(tmp_path)
    h.persona.overrides["Что должен делать бот"] = PersonaTurn(
        decision=PersonaDecision.REPLY, text="Поставьте модуль для каналов, версия 0.1.2."
    )

    await h.buyer().run()

    assert (_verdict(h)["reason"], _verdict(h)["detail"]["reason"]) == (
        "persona_deviation",
        "implementation_instruction",
    )
    assert not any("модуль" in text for text in _codegen_sends(h))


# --- BLOCKER-unrelated-owned-project ------------------------------------------------


async def test_a_same_owner_project_without_this_orders_token_is_never_adopted(tmp_path):
    h = harness(tmp_path)
    h.world.users[BUYER] = {"id": USER_ID}
    nothing = _po_that_creates_nothing(h.world)

    def another_operation_creates_a_project(text):
        if text == OPENING:
            h.world.create_project(SIDE_PROJECT, None, run="unrelated-operation")
            return ["Пришлите, пожалуйста, токен бота."]
        return nothing(text)

    h.world.codegen_answer = another_operation_creates_a_project

    outcome = await h.buyer().run()

    assert _verdict(h)["reason"] == "project_not_proven"
    assert _verdict(h)["detail"]["unproven_candidates"] == [SIDE_PROJECT]
    assert _verdict(h)["detail"]["token_source"] == "env:BUYER_PRODUCT_BOT_TOKEN"  # noqa: S105
    assert "project_id" not in h.store.record["ids"]
    assert h.world.rollout_writes == []
    assert outcome.cleanup == "nothing_owned"
    assert "request_teardown" not in h.api.calls
    assert TOKEN not in h.evidence_text()


async def test_cleanup_refuses_a_project_whose_retained_proof_no_longer_holds(tmp_path):
    h = harness(tmp_path)
    h.world.story_final = "waiting_human_review"

    def the_token_moves_to_another_holder():
        if h.world.stage == "ordered":
            h.world.secrets[PROJECT] = {"TELEGRAM_BOT_TOKEN": "someone-else"}

    h.clock.hooks.append(the_token_moves_to_another_holder)

    outcome = await h.buyer().run()

    assert outcome.cleanup == "refused"
    assert h.store.record["cleanup"]["reason"] == "ownership_unproven"
    assert "request_teardown" not in h.api.calls


# --- BLOCKER-qa-conversation-overlap ---------------------------------------------------


async def test_a_qa_run_holding_the_account_before_registration_gets_no_buyer_message(tmp_path):
    h = harness(tmp_path)
    h.world.busy_qa.append({"id": "other-qa", "status": "running"})

    await h.buyer().run()

    assert [e for e in h.telegram.events if e[0] in {"send", "connect"}] == []
    assert _verdict(h)["reason"] == "qa_never_quiet"


async def test_a_qa_run_starting_mid_order_makes_the_buyer_step_out_until_it_ends(tmp_path):
    h = harness(tmp_path)
    window = {}

    def qa_starts_after_the_opening(text):
        replies = type(h.world).codegen_answer(h.world, text)
        if text == OPENING:
            h.world.busy_qa.append({"id": "other-qa", "status": "running"})
            window["started"] = h.clock.now
        return replies

    def qa_ends():
        if window and h.clock.now >= window["started"] + timedelta(seconds=90):
            h.world.busy_qa.clear()

    h.world.codegen_answer = qa_starts_after_the_opening
    h.clock.hooks.append(qa_ends)

    outcome = await h.buyer().run()

    assert outcome.verdict == "passed", _verdict(h)
    assert h.world.sends_while_qa_busy == []
    assert h.world.connects_while_qa_busy == 0
    assert h.world.reads_while_qa_busy == 0
    assert any(d.get("event") == "qa_yielded" for d in _decisions(h))


async def test_the_product_is_not_probed_while_a_qa_run_holds_the_account(tmp_path):
    h = harness(tmp_path)

    def qa_starts_when_story_completes():
        if h.world.stories and h.world.stories[0]["status"] == "completed" and not h.world.busy_qa:
            h.world.busy_qa.append({"id": "qa-other", "status": "running"})
            h.clock.hooks.remove(qa_starts_when_story_completes)
            h.clock.hooks.append(qa_ends)

    def qa_ends():
        h.world.busy_qa.clear()
        h.clock.hooks.remove(qa_ends)

    h.clock.hooks.append(qa_starts_when_story_completes)

    outcome = await h.buyer().run()

    assert outcome.verdict == "passed", _verdict(h)
    assert h.world.sends_while_qa_busy == []
    assert h.world.connects_while_qa_busy == 0


async def test_a_qa_run_starting_before_the_language_reconnection_is_waited_out(tmp_path):
    h = harness(tmp_path)
    original = h.platform.switch_language

    async def qa_starts_with_the_switch(project_id, deployed_url, language):
        h.world.busy_qa.append({"id": "qa-language", "status": "queued"})
        h.clock.hooks.append(h.world.busy_qa.clear)
        return await original(project_id, deployed_url, language)

    h.platform.switch_language = qa_starts_with_the_switch

    outcome = await h.buyer().run()

    assert outcome.verdict == "passed", _verdict(h)
    assert h.world.sends_while_qa_busy == []
    assert h.world.connects_while_qa_busy == 0
    assert any(d.get("event") == "qa_yielded" for d in _decisions(h))


async def test_a_qa_run_that_never_ends_is_a_bounded_failure_without_probes(tmp_path):
    h = harness(tmp_path)

    def qa_arrives_during_the_build():
        if h.world.stage == "ordered":
            h.world.busy_qa[:] = [{"id": "qa-stuck", "status": "queued"}]

    h.clock.hooks.append(qa_arrives_during_the_build)

    await h.buyer().run()

    assert (_verdict(h)["reason"], _verdict(h)["detail"]) == (
        "qa_never_quiet",
        {"qa_run_ids": ["qa-stuck"]},
    )
    assert h.world.sends_while_qa_busy == []
    assert [e for e in h.telegram.events if e[0] == "send" and e[1] == PRODUCT.username] == []


# --- BLOCKER-resume-duplicate-order and BLOCKER-resume-secret-redaction --------------


async def test_an_interrupted_accepted_send_before_its_receipt_is_not_sent_again(tmp_path):
    h = harness(tmp_path)
    h.world.users[BUYER] = {"id": USER_ID}
    original_send = h.telegram.send

    async def die_after_delivery(peer, text):
        h.telegram.events.append(("send", peer.username, text))
        h.world.append(peer, text, out=True)
        raise ProcessDied

    h.telegram.send = die_after_delivery
    with pytest.raises(ProcessDied):
        await h.buyer().run()
    assert h.store.record["pending"]["kind"] == "order_opening"
    h.telegram.send = original_send
    h.resume(tmp_path)
    h.world.append(CODEGEN, "Отлично! Пришлите, пожалуйста, токен бота от @BotFather.")

    outcome = await h.buyer().run()

    assert outcome.verdict == "passed", _verdict(h)
    assert _codegen_sends(h).count(OPENING) == 1
    assert any(d.get("event") == "intent_reconciled" for d in _decisions(h))


async def test_a_send_never_found_is_an_unknown_delivery_that_blocks_cleanup(tmp_path):
    h = harness(tmp_path)
    original_send = h.telegram.send

    async def die_before_the_confirmation_leaves(peer, text):
        if text == "Да, всё верно.":
            raise ProcessDied
        return await original_send(peer, text)

    h.telegram.send = die_before_the_confirmation_leaves
    with pytest.raises(ProcessDied):
        await h.buyer().run()
    h.telegram.send = original_send
    h.resume(tmp_path)

    outcome = await h.buyer().run()

    assert (_verdict(h)["reason"], outcome.cleanup) == ("delivery_unknown", "refused")
    assert h.store.record["cleanup"]["reason"] == "unresolved_action"
    assert "Да, всё верно." not in _codegen_sends(h)
    assert "request_teardown" not in h.api.calls


async def test_a_delayed_promo_echo_after_a_new_process_stays_redacted(tmp_path):
    h = harness(tmp_path)
    original_send = h.telegram.send

    async def die_after_redemption(peer, text):
        await original_send(peer, text)
        h.world.append(CODEGEN, "Промокод активирован: " + PROMO)
        raise ProcessDied

    h.telegram.send = die_after_redemption
    with pytest.raises(ProcessDied):
        await h.buyer().run()
    h.telegram.send = original_send
    h.resume(tmp_path)

    outcome = await h.buyer().run()

    shown = json.dumps([vars(c) for c in h.persona.contexts], ensure_ascii=False)
    assert PROMO not in shown
    assert PROMO not in h.evidence_text()
    assert h.store.record["registration"]["mode"] == "redeemed"
    assert outcome.verdict == "passed", _verdict(h)


async def test_an_interrupted_promo_mint_is_found_instead_of_minted_twice(tmp_path):
    h = harness(tmp_path)
    original = h.api.mint_promo

    async def mint_then_die(**policy):
        await original(**policy)
        raise ProcessDied

    h.api.mint_promo = mint_then_die
    with pytest.raises(ProcessDied):
        await h.buyer().run()
    assert h.store.record["pending"]["kind"] == "promo_mint"
    h.api.mint_promo = original
    h.resume(tmp_path)

    outcome = await h.buyer().run()

    assert outcome.verdict == "passed", _verdict(h)
    assert len(h.world.promos) == 1
    assert _codegen_sends(h).count(PROMO) == 1


async def test_an_interrupted_order_resumes_without_a_second_order(tmp_path):
    h = harness(tmp_path)
    h.persona.interrupt_on = "готовым решением"

    with pytest.raises(ProcessDied):
        await h.buyer().run()
    h.resume(tmp_path)
    h.persona.interrupt_on = None

    outcome = await h.buyer().run()

    assert outcome.verdict == "passed", _verdict(h)
    sends = _codegen_sends(h)
    assert (sends.count(PROMO), sends.count(TOKEN), sends.count(OPENING)) == (1, 1, 1)
    assert len(h.world.rollout_writes) == 1


async def test_secrets_reach_codegen_but_never_the_persona_or_the_evidence(tmp_path):
    h = harness(tmp_path)

    await h.buyer().run()

    shown = json.dumps([vars(context) for context in h.persona.contexts], ensure_ascii=False)
    retained = h.evidence_text()
    for secret in (TOKEN, PROMO, SESSION, API_HASH, INTERNAL_KEY, "install-op-secret-token"):
        assert secret not in shown
        assert secret not in retained
    assert h.store.record["product_token"]["source"] == "env:BUYER_PRODUCT_BOT_TOKEN"


async def test_a_send_whose_receipt_is_lost_is_found_not_resent(tmp_path):
    h = harness(tmp_path)
    h.world.users[BUYER] = {"id": USER_ID}
    h.telegram.fail_next_sends = 1

    outcome = await h.buyer().run()

    assert outcome.verdict == "passed", _verdict(h)
    assert _codegen_sends(h).count(OPENING) == 1
    assert any(d.get("event") == "send_receipt_lost" for d in _decisions(h))


# --- BLOCKER-deploy-provenance and BLOCKER-install-glue-proof --------------------------


@pytest.mark.parametrize(
    ("change", "observation", "status"),
    [
        ("deploy_without_provenance", "deploy_success", "unknown"),
        ("publication_failed", "deploy_success", "failed"),
        ("deployed_another_commit", "deploy_success", "failed"),
        ("install_on_another_base", "install_after_scaffold", "failed"),
        ("install_without_verification", "install_after_scaffold", "unknown"),
        ("repository_unreadable", "install_after_scaffold", "unknown"),
        ("engineering_beyond_glue", "engineering_glue_only", "failed"),
    ],
)
async def test_uncorrelated_install_glue_and_deploy_evidence_never_passes(
    tmp_path, change, observation, status
):
    h = harness(tmp_path)
    world = h.world
    if change == "deploy_without_provenance":
        world.deploy_result = {**world.deploy_result, "deployment_result": None}
    elif change == "publication_failed":
        world.publication = replace(world.publication, conclusion="failure")
    elif change == "deployed_another_commit":
        world.publication = replace(world.publication, head_sha="9" * 40)
        world.deploy_result["deployment_result"]["deployed_commit_sha"] = "9" * 40
    elif change == "install_on_another_base":
        world.operation = install_operation(base_sha="8" * 40)
    elif change == "install_without_verification":
        world.operation = install_operation(verification=None)
    elif change == "repository_unreadable":
        h.repository.unavailable = {"compare", "pull_request", "workflow_run"}
    else:
        world.engineering_files = ("services/backend/src/app/channels.py",)

    outcome = await h.buyer().run()

    assert _observation(h, observation) == status, h.store.record["observations"][observation]
    assert outcome.verdict in {"failed", "incomplete"}
    assert outcome.exit_code == 1
    assert outcome.cleanup == "completed"


# --- BLOCKER-unsolicited-post-proof ------------------------------------------------------


async def test_a_delayed_command_reply_linking_a_new_post_is_not_unsolicited(tmp_path):
    h = harness(tmp_path)
    h.world.post_after_digest = False
    digest = {}

    def remember(text):
        if text == "/digest":
            digest["at"] = h.clock.now

    def delayed_digest_chunk():
        if not digest or h.clock.now < digest["at"] + timedelta(seconds=60):
            return
        h.clock.hooks.remove(delayed_digest_chunk)
        url = "https://t.me/chan_two/150"
        h.world.channel_posts[("chan_two", 150)] = digest["at"] + timedelta(seconds=1)
        command = [m for m in h.world.dialogs[PRODUCT.id] if m.text == "/digest"][-1]
        h.world.append(PRODUCT, "Подборка (продолжение): " + url, urls=(url,), reply_to=command.id)

    _wrap_product(h, remember)
    h.clock.hooks.append(delayed_digest_chunk)

    outcome = await h.buyer().run()

    observation = h.store.record["observations"]["post_delivered"]
    assert observation["status"] == "unknown"
    assert observation["detail"]["unattributed"] == [
        {"message_id": observation["detail"]["unattributed"][0]["message_id"], "reason": "a reply"}
    ]
    assert outcome.verdict == "incomplete"


async def test_a_late_message_linking_a_post_older_than_the_command_is_not_delivery(tmp_path):
    h = harness(tmp_path)
    h.world.post_after_digest = False
    digest = {}

    def remember(text):
        if text == "/digest":
            digest["at"] = h.clock.now

    def late_old_post():
        if not digest or h.clock.now < digest["at"] + timedelta(seconds=120):
            return
        h.clock.hooks.remove(late_old_post)
        url = "https://t.me/chan_two/120"
        h.world.channel_posts[("chan_two", 120)] = digest["at"] - timedelta(hours=2)
        h.world.append(PRODUCT, "@chan_two " + url, urls=(url,))

    _wrap_product(h, remember)
    h.clock.hooks.append(late_old_post)

    await h.buyer().run()

    observation = h.store.record["observations"]["post_delivered"]
    assert observation["status"] == "unknown"
    assert observation["detail"]["unattributed"][0]["url"] == "https://t.me/chan_two/120"


# --- BLOCKER-reader-counters and the other live facts ----------------------------------


@pytest.mark.parametrize(
    ("change", "observation", "status"),
    [
        ("no_post", "post_delivered", "unknown"),
        ("auth_revoked", "auth_key_active", "failed"),
        ("no_reader_activity", "reader_usage", "unknown"),
        ("another_products_usage", "reader_usage", "failed"),
        ("no_language", "language_switched", "failed"),
    ],
)
async def test_a_missing_or_contradicted_live_fact_never_passes(
    tmp_path, change, observation, status
):
    h = harness(tmp_path)
    if change == "no_post":
        h.world.post_after_digest = False
    elif change == "auth_revoked":
        h.world.auth = AuthFacts("orch-x", "abcdefghijk2", (), ("abcdefghijk2",))
    elif change == "no_reader_activity":
        h.world.usage = UsageFacts(platform_product_id(PROJECT), 2, 0, 0)
    elif change == "another_products_usage":
        h.world.usage = UsageFacts(platform_product_id(UNRELATED_PROJECT), 2, 5, 5)
    else:
        h.world.language_switch_works = False

    outcome = await h.buyer().run()

    assert _observation(h, observation) == status
    assert outcome.verdict in {"failed", "incomplete"}
    assert outcome.exit_code == 1
    assert outcome.cleanup == "completed"


# --- bounded stops ------------------------------------------------------------------------


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


async def test_an_unanswered_order_stalls_with_its_last_turns(tmp_path):
    h = harness(tmp_path)
    h.world.users[BUYER] = {"id": USER_ID}
    h.world.codegen_answer = lambda text: []

    await h.buyer().run()

    verdict = _verdict(h)
    assert (verdict["failure_phase"], verdict["reason"]) == ("order", "conversation_stalled")
    assert verdict["detail"]["last"][-1]["direction"] == "out"


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


# --- cleanup ----------------------------------------------------------------------------


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


# --- BotFather ------------------------------------------------------------------------------

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
    h.resume(tmp_path)

    outcome = await h.buyer(product_token=BOTFATHER_MODE).run()

    assert outcome.verdict == "passed", _verdict(h)
    assert _botfather_sends(h).count("/newbot") == 1
    assert len(h.world.botfather_bots) == 1
    assert "/token" in _botfather_sends(h)
