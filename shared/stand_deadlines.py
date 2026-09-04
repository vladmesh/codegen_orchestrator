"""One deadline ledger shared by the stand runner and its Product Brief fixture."""

MEGA_BRIEF_PRODUCTIVE_SECONDS = 50 * 60
MEGA_BRIEF_HARD_STOP_SECONDS = 60 * 60

if MEGA_BRIEF_HARD_STOP_SECONDS <= MEGA_BRIEF_PRODUCTIVE_SECONDS:
    raise RuntimeError("mega-brief hard stop must leave time for cooperative cleanup")
