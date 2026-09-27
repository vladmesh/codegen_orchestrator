"""The compact platform capability manifest the PO and the Architect read.

`docs/platform_capabilities.yaml` is the source. `python -m scripts.platform_capabilities`
renders this block into `platform_capabilities.txt` beside this module, because the
langgraph image carries neither `docs/` nor the renderer; a unit test fails while the
committed text differs from what the source renders. It is its own budgeted block, kept
out of the PO's capped `SYSTEM_PROMPT` and appended to each agent's model input.
"""

from pathlib import Path

PLATFORM_CAPABILITIES_PROMPT = (
    Path(__file__).with_name("platform_capabilities.txt").read_text(encoding="utf-8").rstrip("\n")
)
