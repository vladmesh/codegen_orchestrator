"""Port-role metadata shared by deploy producers and live harnesses."""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum

from shared.contracts.dto.project import ServiceModule


class PortServiceRole(StrEnum):
    """How a named port allocation is used by deployment checks."""

    HTTP_HEALTH = "http_health"
    NON_HTTP = "non_http"


DEPLOY_INFRA_PORT_SERVICES = ("postgres", "redis")

SERVICE_MODULE_PORT_ROLES: Mapping[ServiceModule, PortServiceRole] = {
    ServiceModule.BACKEND: PortServiceRole.HTTP_HEALTH,
    ServiceModule.TG_BOT: PortServiceRole.NON_HTTP,
    ServiceModule.NOTIFICATIONS: PortServiceRole.NON_HTTP,
    ServiceModule.FRONTEND: PortServiceRole.NON_HTTP,
}

HTTP_HEALTH_PORT_SERVICES = frozenset(
    module.value
    for module, role in SERVICE_MODULE_PORT_ROLES.items()
    if role is PortServiceRole.HTTP_HEALTH
)


def is_http_health_port_service(service_name: object) -> bool:
    return service_name in HTTP_HEALTH_PORT_SERVICES
