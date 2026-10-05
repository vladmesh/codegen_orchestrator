from datetime import datetime
from typing import Literal

from pydantic import ConfigDict, model_validator

from shared.contracts.base import BaseMessage
from shared.contracts.dto.catalog_install import CatalogInstall
from shared.contracts.template import ServiceTemplateRef, ServiceTemplateSource


class ScaffoldMessage(BaseMessage):
    """Trigger scaffolding for a project repository.

    Published by scheduler for both new (draft) and existing (active) projects.
    Consumed by scaffolder service.

    Modes:
        full: Full scaffold: copier + make setup + git push (new projects).
        ensure: Verify workspace exists; if missing, clone + setup (existing projects).
        install: Execute the exclusively claimed typed catalog operation (existing products).
    """

    project_id: str
    repository_id: str
    # Telegram chat of the project owner, resolved by the producer.
    telegram_chat_id: str = ""
    template_repo: ServiceTemplateSource
    template_ref: ServiceTemplateRef
    project_name: str  # sanitized name for copier
    modules: str  # comma-separated, e.g. "backend,tg_bot"
    task_description: str = ""
    model_config = ConfigDict(extra="forbid")

    mode: Literal["full", "ensure", "install"] = "full"
    task_id: str | None = None
    story_id: str | None = None
    operation_id: str | None = None
    cycle_started_at: datetime | None = None
    install: CatalogInstall | None = None

    @model_validator(mode="after")
    def install_ownership(self):
        values = (
            self.task_id,
            self.story_id,
            self.operation_id,
            self.cycle_started_at,
            self.install,
        )
        if self.mode == "install":
            if any(value is None for value in values) or not all(
                (
                    self.project_id,
                    self.repository_id,
                    self.task_id,
                    self.story_id,
                    self.operation_id,
                )
            ):
                raise ValueError("install requires task/story/repository/cycle/operation/payload")
        elif any(value is not None for value in values):
            raise ValueError("install ownership is only valid for install mode")
        return self
