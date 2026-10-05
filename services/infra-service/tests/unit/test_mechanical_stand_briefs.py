"""Mechanical stand proposals must pass the released Product Brief write boundary."""

from contextlib import nullcontext
from pathlib import Path
import sys
from types import ModuleType
from unittest.mock import AsyncMock

import pytest

from shared.contracts.dto.product_brief import ProposedProductBriefContent


@pytest.fixture
def stand(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[4] / "tests/live"))
    from level1_brief import build_level1_brief
    import mechanical_install
    import mechanical_notes

    return mechanical_install, mechanical_notes, build_level1_brief


def proposed_content(brief):
    arguments = brief.present_arguments("11111111-1111-1111-1111-111111111111")
    arguments.pop("project_id")
    arguments.pop("title")
    return ProposedProductBriefContent.model_validate(arguments)


def test_notes_brief_can_be_presented_to_the_po(stand):
    _, notes, build_brief = stand
    ctx = {"level1_marker": "stand-marker", "level1_brief": build_brief("stand-marker")}
    notes.configure_notes(ctx)

    content = proposed_content(ctx["level1_brief"])
    assert [(one.user_sends, one.product_answers) for one in content.usage_examples] == [
        ("/note keep this", "Saved: keep this"),
        ("/notes", "keep this"),
    ]


async def test_install_brief_can_be_presented_to_the_po(stand, monkeypatch):
    install, _, _ = stand
    # Stop at presentation; native readbacks and deployment are CI-only boundaries.
    pipeline = ModuleType("test_full_pipeline")
    pipeline._level1_extension_owner_told = AsyncMock()
    pipeline._require_level1_merge_artifact = AsyncMock()
    monkeypatch.setitem(sys.modules, "test_full_pipeline", pipeline)
    monkeypatch.setattr(install, "qa_probe", lambda ctx: {})
    monkeypatch.setattr(install, "readback", lambda *args: {})
    monkeypatch.setattr(install, "check_readback", lambda *args, **kwargs: None)
    monkeypatch.setattr(install, "revoke_readback", AsyncMock(return_value={}))
    monkeypatch.setattr(install, "install_scope", lambda ctx: nullcontext())
    presented = []

    class PresentationChecked(Exception):
        pass

    async def present(api, ctx):
        presented.append(proposed_content(ctx["level1_brief"]))
        raise PresentationChecked

    monkeypatch.setattr(install.h, "create_level1_confirmed_brief", present)
    ctx = {
        "mechanical_acceptance": {},
        "deploy_merge_commit_sha": "a" * 40,
        "deployed_image_references": {},
        "level1_marker": "stand-marker",
    }
    with pytest.raises(PresentationChecked):
        await install.native_second_story(None, None, None, ctx, debug_prefix="test")

    assert [(one.user_sends, one.product_answers) for one in presented[0].usage_examples] == [
        ("/remind buy milk in 2 minutes", "Scheduled for a word-month instant: buy milk"),
    ]
