"""The one policy resolver for engineering and QA paid-run executors."""

from collections.abc import Mapping

from shared.contracts.dto.executor_decision import (
    ExecutorDecision,
    ExecutorDecisionSource,
    ExecutorOverride,
)
from shared.contracts.dto.run import RunType
from shared.contracts.vocab import QA_EXECUTOR_AGENT_TYPES, AgentType

from .config import Settings


class ExecutorResolutionError(ValueError):
    """The paid-start input cannot produce one valid executor decision."""


def _agent_type(value: object, *, name: str) -> AgentType:
    try:
        return AgentType(value)
    except (TypeError, ValueError) as exc:
        raise ExecutorResolutionError(f"{name} must name a supported agent type") from exc


def resolve_executor_decision(
    attempt_kind: RunType,
    project_config: Mapping[str, object] | None,
    settings: Settings,
    *,
    global_override: ExecutorOverride = ExecutorOverride.NONE,
) -> ExecutorDecision:
    """Resolve exactly one persisted executor choice before a paid Run exists."""
    config = project_config or {}
    if not isinstance(config, Mapping):
        raise ExecutorResolutionError("project config must be an object")

    try:
        override = ExecutorOverride(global_override)
    except (TypeError, ValueError) as exc:
        raise ExecutorResolutionError(
            "global executor override must be none, claude, or codex"
        ) from exc

    if override is not ExecutorOverride.NONE:
        if attempt_kind not in {RunType.ENGINEERING, RunType.QA}:
            raise ExecutorResolutionError(
                f"Paid executor selection does not support {attempt_kind.value}"
            )
        return ExecutorDecision(
            attempt_kind=attempt_kind,
            agent_type=AgentType(override.value),
            source=ExecutorDecisionSource.GLOBAL_OVERRIDE,
            policy_version="v2",
            reason=f"Global break-glass override selected {attempt_kind.value} executor.",
        )

    if attempt_kind is RunType.ENGINEERING:
        pin = config.get("agent_type")
        if pin is not None:
            agent_type = _agent_type(pin, name="project config agent_type")
            return ExecutorDecision(
                attempt_kind=attempt_kind,
                agent_type=agent_type,
                source=ExecutorDecisionSource.PROJECT_PIN,
                policy_version="v2",
                reason="Engineering executor pinned by project configuration.",
            )
        agent_type = _agent_type(settings.default_agent_type, name="DEFAULT_AGENT_TYPE")
        return ExecutorDecision(
            attempt_kind=attempt_kind,
            agent_type=agent_type,
            source=ExecutorDecisionSource.API_DEFAULT,
            policy_version="v2",
            reason="Engineering executor selected by API DEFAULT_AGENT_TYPE.",
        )

    if attempt_kind is RunType.QA:
        agent_type = _agent_type(settings.qa_executor_agent_type, name="QA_EXECUTOR_AGENT_TYPE")
        if agent_type not in QA_EXECUTOR_AGENT_TYPES:
            allowed = ", ".join(sorted(agent.value for agent in QA_EXECUTOR_AGENT_TYPES))
            raise ExecutorResolutionError(
                f"QA_EXECUTOR_AGENT_TYPE must be one of {allowed}, not {agent_type.value}"
            )
        return ExecutorDecision(
            attempt_kind=attempt_kind,
            agent_type=agent_type,
            source=ExecutorDecisionSource.QA_API_SETTING,
            policy_version="v2",
            reason="QA executor selected by API QA_EXECUTOR_AGENT_TYPE.",
        )

    raise ExecutorResolutionError(f"Paid executor selection does not support {attempt_kind.value}")
