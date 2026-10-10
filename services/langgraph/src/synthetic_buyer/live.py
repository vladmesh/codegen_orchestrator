"""The live ports: built only by the `run`, `resume` and `cleanup` commands.

Nothing here runs on import. Each port resolves its own credentials from their
handles when it is built or used, and adds every resolved value to the
operation's redaction set before anything could echo it.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

from shared.crypto import decrypt_dict

from .codegen_api import CodegenApi
from .config import BuyerConfig, ModelConfig
from .controller import Clock, SyntheticBuyer
from .evidence import EvidenceStore, Redaction
from .persona import ModelPersona
from .platform_evidence import LivePlatformFacts
from .telegram import TelethonPort

#: Runtime credentials read by existing shared code, not by a handle: the internal
#: API transport's key and the project-secret cipher's key.
INTERNAL_API_KEY_ENV = "INTERNAL_API_KEY"
RUNTIME_KEY_ENV = "SECRETS_ENCRYPTION_KEY"


def persona_model(model: ModelConfig):
    """The persona's chat model: the configured chain over the existing channel adapters.

    It is built under the PO summarizer's agent identity, which shares the PO's
    OpenRouter endpoint and key but carries no PO operational note.
    """
    from ..config.settings import get_settings  # noqa: PLC0415 - live mode only
    from ..llm import LLMAgent, build_agent_llm  # noqa: PLC0415

    return build_agent_llm(LLMAgent.PO_SUMMARIZER, list(model.chain), get_settings())


def operation_directory(config: BuyerConfig) -> Path:
    return Path(config.evidence_dir) / config.operation_id


async def _sleep(seconds: float) -> None:
    await asyncio.sleep(seconds)


@asynccontextmanager
async def live_buyer(config: BuyerConfig, store: EvidenceStore, environ: Mapping[str, str]):
    """The controller over the production ports, closed on exit."""
    redaction = store.redaction
    redaction.add(environ.get(INTERNAL_API_KEY_ENV), environ.get(RUNTIME_KEY_ENV))
    api = CodegenApi(config.api.base_url)

    async def stored_secrets(project_id: str) -> dict:
        project = await api.project(project_id)
        stored = (project.get("config") or {}).get("secrets") or {}
        return decrypt_dict(stored) if stored else {}

    try:
        yield SyntheticBuyer(
            config,
            telegram=TelethonPort(config.telegram, environ, redaction),
            api=api,
            persona=ModelPersona(persona_model(config.model), config.scenario),
            platform=LivePlatformFacts(
                config.platform, environ, redaction, stored_secrets=stored_secrets
            ),
            store=store,
            clock=Clock(wall=lambda: datetime.now(UTC), sleep=_sleep),
            environ=environ,
        )
    finally:
        await api.aclose()


def new_store(config: BuyerConfig) -> EvidenceStore:
    return EvidenceStore(operation_directory(config), Redaction())
