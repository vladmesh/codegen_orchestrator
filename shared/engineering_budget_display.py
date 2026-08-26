"""Presentation helpers for ledger-derived engineering budget amounts."""

MICROUSD_PER_USD = 1_000_000
MIN_DISPLAY_DECIMALS = 2


def format_microusd(value: int) -> str:
    """Render integer micro-USD exactly, without float rounding."""
    dollars, micros = divmod(value, MICROUSD_PER_USD)
    fraction = f"{micros:06d}".rstrip("0")
    if len(fraction) < MIN_DISPLAY_DECIMALS:
        fraction = fraction.ljust(MIN_DISPLAY_DECIMALS, "0")
    return f"${dollars}.{fraction}"
