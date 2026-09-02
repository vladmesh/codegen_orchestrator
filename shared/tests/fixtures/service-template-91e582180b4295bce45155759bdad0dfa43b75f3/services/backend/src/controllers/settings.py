"""Implementation of the versioned, manifest-backed core settings contract."""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException, status
from jsonschema import Draft202012Validator
from sqlalchemy.ext.asyncio import AsyncSession

from services.backend.src.app.models.setting import Setting, SettingScope
from services.backend.src.app.repositories.setting import SettingRepository
from services.backend.src.generated.protocols import SettingsControllerProtocol
from services.backend.src.generated.settings_schemas import SETTINGS_SCHEMAS
from shared.generated.schemas import Scope as ContractScope, SettingGet, SettingSet, SettingValue

CORE_SETTINGS_CONTRACT_VERSION = 1


def _contract_scope(scope: ContractScope | str) -> ContractScope:
    """Normalize generated defaults, which Pydantic leaves as their literal."""
    return ContractScope(scope)


def _storage_scope(scope: ContractScope | str) -> SettingScope:
    return SettingScope(_contract_scope(scope).value)


def _subject_id(scope: ContractScope | str, subject_id: int | None) -> int:
    normalized_scope = _contract_scope(scope)
    if normalized_scope is ContractScope.product and subject_id is None:
        return 0
    if normalized_scope is ContractScope.user and subject_id is not None:
        return subject_id
    raise HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        detail="subject_id is required only for user-scoped settings",
    )


def _schema_for(key: str) -> dict[str, Any]:
    schema = SETTINGS_SCHEMAS.get(key)
    if not isinstance(schema, dict):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Setting key not declared"
        )
    return schema


def _validate_value(key: str, value: Any) -> None:
    if any(Draft202012Validator(_schema_for(key)).iter_errors(value)):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Setting value does not satisfy its declared schema",
        )


def _to_contract(setting: Setting) -> SettingValue:
    return SettingValue(
        contract_version=CORE_SETTINGS_CONTRACT_VERSION,
        key=setting.key,
        scope=ContractScope(setting.scope.value),
        subject_id=setting.subject_id or None,
        value=setting.value,
    )


class SettingsController(SettingsControllerProtocol):
    """Read and write only manifest-declared, schema-valid product settings."""

    async def get(self, session: AsyncSession, payload: SettingGet) -> SettingValue:
        setting = await SettingRepository(session).get(
            payload.key,
            _storage_scope(payload.scope),
            _subject_id(payload.scope, payload.subject_id),
        )
        if setting is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Setting value not found"
            )
        return _to_contract(setting)

    async def set(self, session: AsyncSession, payload: SettingSet) -> SettingValue:
        _validate_value(payload.key, payload.value)
        setting = await SettingRepository(session).set(
            payload.key,
            _storage_scope(payload.scope),
            _subject_id(payload.scope, payload.subject_id),
            payload.value,
        )
        return _to_contract(setting)
