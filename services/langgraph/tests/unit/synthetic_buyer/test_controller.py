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
from src.synthetic_buyer.repository_evidence import JobFacts
from src.synthetic_buyer.telegram import TransportError
from tests.unit.synthetic_buyer.fakes import (
    API_HASH,
    BRIEF,
    BUYER,
    CODEGEN,
    DEPLOY_WORKFLOW_RUN,
    DIGEST,
    IMAGE,
    INTERNAL_KEY,
    MERGE,
    PREVIEW,
    PRODUCT,
    PROJECT,
    PROMO,
    PUBLICATION_RUN,
    SESSION,
    SIDE_PROJECT,
    START,
    STRANGER,
    TOKEN,
    UNRELATED_PROJECT,
    USER_ID,
    harness,
    install_operation,
    post_event,
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
    assert h.store.record["teardown"]["project_status"] == "archived"
    assert PROJECT not in h.world.projects
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


async def test_a_session_whose_setup_fails_after_connect_is_still_disconnected(tmp_path):
    h = harness(tmp_path)
    original = h.telegram.connect

    async def connected_then_refused():
        await original()
        raise TransportError("authorization", "did not answer in 30s")

    h.telegram.connect = connected_then_refused
    result = await h.buyer().run()

    assert _verdict(h)["reason"] == "TransportError"
    assert not h.telegram.connected
    assert [e for e in h.telegram.events if e[0] == "disconnect"]
    assert result.cleanup == "nothing_owned"
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


CONFIRMATION = "Да, всё верно."


# --- BLOCKER-deploy-provenance and BLOCKER-install-glue-proof --------------------------


@pytest.mark.parametrize(
    ("change", "observation", "status"),
    [
        ("deploy_without_provenance", "deploy_success", "unknown"),
        ("publication_failed", "deploy_success", "failed"),
        ("no_publication_of_the_commit", "deploy_success", "failed"),
        ("main_yml_mistaken_for_publication", "deploy_success", "failed"),
        ("deploy_run_given_as_publication", "deploy_success", "failed"),
        ("build_and_push_failed", "deploy_success", "failed"),
        ("deploy_run_failed", "deploy_success", "failed"),
        ("deploy_run_of_another_commit", "deploy_success", "failed"),
        ("deployed_another_commit", "deploy_success", "failed"),
        ("image_of_another_commit", "deploy_success", "failed"),
        ("timeline_names_another_publication", "deploy_success", "failed"),
        ("actions_unreadable", "deploy_success", "unknown"),
        ("install_on_another_base", "install_after_scaffold", "failed"),
        ("install_without_verification", "install_after_scaffold", "unknown"),
        ("repository_unreadable", "install_after_scaffold", "unknown"),
        ("engineering_beyond_glue", "engineering_glue_only", "failed"),
    ],
)
async def test_uncorrelated_install_glue_and_deploy_evidence_never_passes(  # noqa: C901, PLR0912
    tmp_path, change, observation, status
):
    h = harness(tmp_path)
    world = h.world
    runs = world.workflow_runs
    placed = world.deploy_result["deployment_result"]
    if change == "deploy_without_provenance":
        world.deploy_result = {**world.deploy_result, "deployment_result": None}
    elif change == "publication_failed":
        runs[PUBLICATION_RUN] = replace(runs[PUBLICATION_RUN], conclusion="failure")
    elif change == "no_publication_of_the_commit":
        del runs[PUBLICATION_RUN]
    elif change == "main_yml_mistaken_for_publication":
        runs[PUBLICATION_RUN] = replace(runs[PUBLICATION_RUN], path=".github/workflows/main.yml")
    elif change == "deploy_run_given_as_publication":
        # The seed's mistake: the deploy.yml run id read as the publication run.
        runs[DEPLOY_WORKFLOW_RUN] = replace(
            runs[DEPLOY_WORKFLOW_RUN], path=".github/workflows/ci.yml", head_branch="main"
        )
        del runs[PUBLICATION_RUN]
    elif change == "build_and_push_failed":
        world.jobs[PUBLICATION_RUN] = [
            JobFacts(
                "build-and-push (backend, ., services/backend/Dockerfile, backend)",
                "completed",
                "failure",
            ),
        ]
    elif change == "deploy_run_failed":
        runs[DEPLOY_WORKFLOW_RUN] = replace(runs[DEPLOY_WORKFLOW_RUN], conclusion="failure")
    elif change == "deploy_run_of_another_commit":
        runs[DEPLOY_WORKFLOW_RUN] = replace(runs[DEPLOY_WORKFLOW_RUN], head_sha="9" * 40)
    elif change == "deployed_another_commit":
        placed["deployed_commit_sha"] = "9" * 40
    elif change == "image_of_another_commit":
        placed["image_references"] = {"BACKEND_IMAGE": IMAGE.rsplit(":", 1)[0] + ":sha-9999999"}
    elif change == "timeline_names_another_publication":
        world.timeline_publication_id = 9002
    elif change == "actions_unreadable":
        h.repository.unavailable = {"publication_runs", "workflow_jobs"}
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


async def test_the_deploy_run_and_the_publication_are_distinct_runs_of_the_built_commit(tmp_path):
    h = harness(tmp_path)

    await h.buyer().run()

    detail = h.store.record["observations"]["deploy_success"]["detail"]
    assert detail["deploy_workflow_run_id"] == DEPLOY_WORKFLOW_RUN
    assert detail["deploy_workflow_run"]["path"] == ".github/workflows/deploy.yml"
    assert detail["publication"]["id"] == PUBLICATION_RUN
    assert detail["publication"]["path"] == ".github/workflows/ci.yml"
    assert detail["publication"]["head_sha"] == detail["deployed_commit_sha"] == MERGE
    assert detail["timeline_publication"]["id"] == PUBLICATION_RUN
    ids = h.store.record["ids"]
    assert (ids["publication_run_id"], ids["deploy_workflow_run_id"]) == (
        PUBLICATION_RUN,
        DEPLOY_WORKFLOW_RUN,
    )


# --- BLOCKER-unsolicited-post-proof ------------------------------------------------------


def _product_sends(h) -> list[str]:
    return [e[2] for e in h.telegram.events if e[0] == "send" and e[1] == PRODUCT.username]


async def test_an_independently_delivered_post_is_proven_before_digest_is_sent(tmp_path):
    h = harness(tmp_path)

    outcome = await h.buyer().run()

    assert outcome.verdict == "passed", _verdict(h)
    post = h.store.record["observations"]["post_delivered"]["detail"]
    assert (post["channel"], post["post_id"], post["form"]) == (
        "chan_two",
        201,
        "tg-channels.post (ru)",
    )
    assert post["source_published_at"] <= post["delivered_at"]
    assert _product_sends(h) == ["/start", "/channels", "/digest", "/channels"]
    digest_sent = next(
        e for e in h.store.record["conversation"]["product"] if e.get("text") == "/digest"
    )
    assert digest_sent["id"] > post["message_id"]
    assert _observation(h, "digest_answered") == "observed"


async def test_a_delayed_unquoted_digest_item_linking_a_fresh_post_is_not_delivery(tmp_path):
    """QA's own `/digest` earlier in the chat; its late item links a post made after it."""
    h = harness(tmp_path)
    h.world.post_event = False
    asked = {}

    async def qa_asks_for_a_digest():
        if h.world.qa_phase == "holding" and "at" not in asked:
            asked["at"] = h.clock.now
            h.world.append(PRODUCT, "/digest", out=True)

    def remember(text):
        if text == "/channels" and "channels" not in asked:
            asked["channels"] = h.clock.now
            h.clock.hooks.append(late_item)

    def late_item():
        # Unquoted, far beyond the settle window, while the buyer waits for a post.
        if h.clock.now < asked["channels"] + timedelta(minutes=4):
            return
        h.clock.hooks.remove(late_item)
        url = "https://t.me/chan_two/150"
        h.world.channel_posts[("chan_two", 150)] = asked["at"] + timedelta(seconds=1)
        h.world.append(PRODUCT, f"@chan_two\n2026-10-10\nСвежий пост\n{url}", urls=(url,))

    h.clock.hooks.append(qa_asks_for_a_digest)
    _wrap_product(h, remember)

    outcome = await h.buyer().run()

    observation = h.store.record["observations"]["post_delivered"]
    assert observation["status"] == "unknown"
    assert [entry["reason"] for entry in observation["detail"]["unattributed"]] == [
        "not the post event's form"
    ]
    assert outcome.verdict == "incomplete"


@pytest.mark.parametrize(
    ("form", "reason"),
    [
        ("reply", "a reply"),
        ("other_channel", "links a post of another channel than it names"),
        ("dated_after_delivery", "the channel does not date the post before its delivery"),
    ],
)
async def test_an_event_form_message_that_does_not_hold_together_is_not_delivery(
    tmp_path, form, reason
):
    h = harness(tmp_path)
    h.world.post_event = False
    sent = {}

    def remember(text):
        if text == "/channels" and "at" not in sent:
            sent["at"] = h.clock.now
            h.clock.hooks.append(deliver)

    def deliver():
        if h.clock.now < sent["at"] + timedelta(seconds=60):
            return
        h.clock.hooks.remove(deliver)
        url = "https://t.me/chan_two/300"
        h.world.channel_posts[("chan_two", 300)] = (
            h.clock.now + timedelta(minutes=5)
            if form == "dated_after_delivery"
            else h.clock.now - timedelta(seconds=10)
        )
        named = "chan_one" if form == "other_channel" else "chan_two"
        command = [m for m in h.world.dialogs[PRODUCT.id] if m.text == "/channels"][-1]
        h.world.append(
            PRODUCT,
            post_event(named, "Пост", url),
            urls=(url,),
            reply_to=command.id if form == "reply" else None,
        )

    _wrap_product(h, remember)

    await h.buyer().run()

    observation = h.store.record["observations"]["post_delivered"]
    assert observation["status"] == "unknown"
    assert [entry["reason"] for entry in observation["detail"]["unattributed"]] == [reason]


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
        h.world.post_event = False
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
    assert (again.verdict, again.cleanup) == ("failed", "refused")
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
