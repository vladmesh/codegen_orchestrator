from src.prompts.po import SYSTEM_PROMPT


def test_po_prompt_offers_only_modules_the_pinned_kit_accepts() -> None:
    module_line = next(line for line in SYSTEM_PROMPT.splitlines() if "Modules:" in line)

    assert "`backend,tg_bot` for bots" in module_line
    assert "`backend` for API only" in module_line
    assert "frontend" not in module_line
    assert "notifications" not in module_line
