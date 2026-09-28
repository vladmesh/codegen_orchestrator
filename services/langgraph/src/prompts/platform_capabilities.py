"""The platform capability blocks: the PO's product block and the Architect's technical one.

`docs/platform_capabilities.yaml` is the source. `python -m scripts.platform_capabilities`
renders both into `platform_capabilities.txt` (the PO: can, cannot and instead in product
language) and `platform_capabilities_architect.txt` (the Architect: how and why in the code)
beside this module, because the langgraph image carries neither `docs/` nor the renderer;
a unit test fails while committed text differs from what the source renders. Each is its own
budgeted block, kept out of the PO's capped `SYSTEM_PROMPT` and appended to the model input.
"""

from pathlib import Path


def _block(name: str) -> str:
    return Path(__file__).with_name(name).read_text(encoding="utf-8").rstrip("\n")


PLATFORM_CAPABILITIES_PROMPT = _block("platform_capabilities.txt")
ARCHITECT_PLATFORM_CAPABILITIES_PROMPT = _block("platform_capabilities_architect.txt")
