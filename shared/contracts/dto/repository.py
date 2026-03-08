"""Repository DTOs and enums."""

from enum import StrEnum


class RepositoryRole(StrEnum):
    PRIMARY = "primary"
    DEPENDENCY = "dependency"


class RepositoryVisibility(StrEnum):
    PUBLIC = "public"
    PRIVATE = "private"
