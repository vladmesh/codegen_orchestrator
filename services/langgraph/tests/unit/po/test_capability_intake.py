"""The Calendar canary is refused before a brief or story exists."""

from unittest.mock import AsyncMock

from langchain_core.messages import AIMessage
import pytest

from shared.notifications import AdminDeliveryResult
from src.agents.po import tools
from src.agents.po.graph import create_po_graph
from src.agents.po.tools_briefs import confirm_product_brief
from src.agents.po.tools_stories import create_story
from tests.unit.po.test_tools_briefs import (
    _API,
    BRIEF_ID,
    PRODUCT_BRIEF_POINTER_KEY,
    PROJECT_ID,
    _brief,
    _config,
    _install,
    _present,
    _stored_content,
)
from tests.unit.test_architect_graph import _ScriptedToolCallingModel

CALENDAR_REQUIREMENTS = [
    {
        "id": "connect",
        "text": "/connect — secure Google OAuth flow",
        "user_wording": "Хочу помощника Google Calendar с безопасной авторизацией через Google",
    }
]
CALENDAR_EXAMPLES = [
    {
        "requirement_id": "connect",
        "user_sends": "/connect",
        "product_answers": "Календарь подключён",
    }
]
WORKAROUND = {
    "feature": "Подключение календаря",
    "chosen": "service account the user shares the calendar with",
    "alternative": "Google OAuth",
    "trade_off": "Нужно вручную открыть календарь сервисному аккаунту.",
    "add_later": "Веб-вход возможен только после изменения платформы.",
    "capability": "oauth_web_redirect",
}


def calendar_content(**overrides):
    return {
        **_stored_content(),
        "summary": "Помощник Google Calendar",
        "language": "ru",
        "must_requirements": CALENDAR_REQUIREMENTS,
        "usage_examples": CALENDAR_EXAMPLES,
        **overrides,
    }


async def test_calendar_canary_refuses_brief_and_cannot_create_story():
    api = _API()
    _install(api, AsyncMock())
    result = await _present(**calendar_content())
    assert "not possible now" in result
    assert "oauth_web_redirect" in result
    assert "service account" in result
    assert "/connect" in result
    assert api.briefs == {} and api.posts == [] and api.patches == []
    refusal = await create_story.ainvoke(
        {"project_id": PROJECT_ID, "title": "Calendar", "description": "Calendar assistant"},
        config=_config(),
    )
    assert "Product Brief" in refusal
    assert api.posts == []


async def test_calendar_with_accepted_workaround_presents_and_confirms():
    api = _API()
    _install(api, AsyncMock())
    result = await _present(**calendar_content(variant_choices=[WORKAROUND]))
    assert "Product Brief revision 1" in result
    assert WORKAROUND["chosen"] in result
    assert api.briefs[BRIEF_ID]["content"]["variant_choices"] == [WORKAROUND]
    await confirm_product_brief.ainvoke(
        {"project_id": PROJECT_ID, "brief_id": BRIEF_ID}, config=_config()
    )
    assert api.briefs[BRIEF_ID]["confirmed_at"] is not None


async def test_conflicting_proposal_is_checked_even_when_a_safe_revision_is_open():
    api = _API(
        project_config={PRODUCT_BRIEF_POINTER_KEY: BRIEF_ID},
        briefs={BRIEF_ID: _brief()},
    )
    _install(api, AsyncMock())
    result = await _present(**calendar_content())
    assert "not possible now" in result and "oauth_web_redirect" in result
    assert api.posts == [] and api.patches == []


@pytest.mark.parametrize("confirmed", [False, True])
async def test_stored_conflicting_brief_cannot_be_confirmed_or_presented(confirmed):
    api = _API(
        project_config={PRODUCT_BRIEF_POINTER_KEY: BRIEF_ID},
        briefs={BRIEF_ID: _brief(content=calendar_content(), confirmed=confirmed)},
    )
    _install(api, AsyncMock())
    result = await confirm_product_brief.ainvoke(
        {"project_id": PROJECT_ID, "brief_id": BRIEF_ID}, config=_config()
    )
    assert "oauth_web_redirect" in result and "not possible now" in result
    result = await _present()
    assert "oauth_web_redirect" in result and "not possible now" in result
    assert api.posts == [] and api.patches == []


async def test_insisting_user_consumer_sends_one_admin_note_and_starts_no_work(monkeypatch):
    from src.consumers.po import _handle_message

    api = _API()
    client = AsyncMock()
    _install(api, client)
    deliver = AsyncMock(return_value=AdminDeliveryResult(configured=1, succeeded=1))
    monkeypatch.setattr(tools, "deliver_to_admins", deliver)
    words = "Сервисный аккаунт не подходит, хочу именно вход через Google"
    model = _ScriptedToolCallingModel(
        turns=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "pass_capability_request",
                        "args": {
                            "project_id": PROJECT_ID,
                            "capability_id": "oauth_web_redirect",
                            "user_words": words,
                        },
                        "id": "pass-1",
                    }
                ],
            ),
            AIMessage(content="Запрос передан администраторам. Работа не начата."),
        ]
    )
    graph = await create_po_graph(llm=model, summarization_llm=model)
    await _handle_message(
        graph,
        client,
        "42",
        {
            "type": "user_message",
            "text": words,
            "timestamp": "2026-09-27T12:00:00Z",
            "request_id": "calendar-insists",
        },
    )
    deliver.assert_awaited_once()
    note = deliver.call_args.args[0]
    assert all(text in note for text in [PROJECT_ID, "oauth_web_redirect", words])
    assert api.posts == [] and api.briefs == {} and api.patches == []
    assert "Работа не начата" in client.publish_flat.call_args.args[1]["text"]


@pytest.mark.parametrize("capability_id", ["unknown", "outbound_apis"])
async def test_request_rejects_ids_outside_cannot_list(capability_id, monkeypatch):
    deliver = AsyncMock()
    monkeypatch.setattr(tools, "deliver_to_admins", deliver)
    result = await tools.pass_capability_request.ainvoke(
        {"project_id": PROJECT_ID, "capability_id": capability_id, "user_words": "Insist"},
        config=_config(),
    )
    assert "Unknown" in result
    deliver.assert_not_awaited()


async def test_failed_admin_delivery_does_not_claim_request_was_passed_on(monkeypatch):
    deliver = AsyncMock(return_value=AdminDeliveryResult(configured=1, succeeded=0))
    monkeypatch.setattr(tools, "deliver_to_admins", deliver)
    result = await tools.pass_capability_request.ainvoke(
        {"project_id": PROJECT_ID, "capability_id": "oauth_web_redirect", "user_words": "Insist"},
        config=_config(),
    )
    assert "did not reach" in result
    assert "No work was started" in result
