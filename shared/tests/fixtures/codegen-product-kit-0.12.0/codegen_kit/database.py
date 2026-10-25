"""Stable database seam for in-process packages."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

_CAPABILITY_KEY = object()


class PackageDatabase:
    """An installed package's validated schema ownership capability."""

    def __init__(self, schema: str, key: object) -> None:
        if key is not _CAPABILITY_KEY:
            raise TypeError("package database capabilities are created by package_database()")
        self._schema = schema

    def base(self) -> type[Any]:
        """Create an independent declarative base in the owned schema."""

        from sqlalchemy import MetaData
        from sqlalchemy.orm import DeclarativeBase

        schema = self._schema

        class PackageBase(DeclarativeBase):
            metadata = MetaData(schema=schema)

        return PackageBase

    @asynccontextmanager
    async def session(self) -> AsyncIterator[Any]:
        """Open a core-owned transaction scoped to the owned schema."""

        from sqlalchemy import text

        from services.backend.src.core.db import AsyncSessionLocal

        async with AsyncSessionLocal() as session:
            try:
                await session.execute(text(f'SET LOCAL search_path TO "{self._schema}", public'))
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise


def owned_package_database(caller_path: Path) -> PackageDatabase:
    """Create a capability after resolving the caller to its installed manifest."""

    from .packages import owned_database_schema

    return PackageDatabase(owned_database_schema(caller_path), _CAPABILITY_KEY)
