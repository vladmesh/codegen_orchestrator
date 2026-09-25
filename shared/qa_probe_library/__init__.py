"""Platform-shipped QA probes, offered to QA runs beside a project's own library.

A seed is a sandbox probe file: the QA executor runs it through `qa probe` like
any probe it wrote, so its source, arguments and output are retained with the
Run. Seeds live here as ordinary files and are read as text; nothing imports
them into a platform process.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from shared.contracts.dto.run_result import QAProbeFileKind, QAProbePlatform
from shared.qa_probe_cli import QA_PROBE_LIBRARY_PATH

__all__ = ["SEED_ORIGIN", "QAProbeSeed", "seed_probes"]

SEED_ORIGIN = "seed"
_ROOT = Path(__file__).parent


@dataclass(frozen=True)
class QAProbeSeed:
    """One platform-shipped probe and the one line that says how to run it."""

    platform: QAProbePlatform
    name: str
    file_kind: QAProbeFileKind
    arguments: str
    #: Offered only to a run whose product has a Telegram bot under test.
    needs_telegram_bot: bool

    @property
    def relative_path(self) -> str:
        return f"{self.platform.value}/{self.name}.{self.file_kind.value}"

    @property
    def usage(self) -> str:
        return (
            f"qa probe {self.platform.value} {self.name} "
            f"{QA_PROBE_LIBRARY_PATH}/{self.relative_path} {self.arguments}"
        )

    def source(self) -> str:
        return (_ROOT / self.relative_path).read_text(encoding="utf-8")


_SEEDS = (
    QAProbeSeed(
        platform=QAProbePlatform.TELEGRAM,
        name="location",
        file_kind=QAProbeFileKind.PY,
        arguments="@BOT LAT LON [WAIT_SECONDS]",
        needs_telegram_bot=True,
    ),
)


def seed_probes(*, telegram_bot: bool) -> list[QAProbeSeed]:
    """The seeds a run is offered: Telegram seeds only when it has a bot to test."""
    return [seed for seed in _SEEDS if telegram_bot or not seed.needs_telegram_bot]
