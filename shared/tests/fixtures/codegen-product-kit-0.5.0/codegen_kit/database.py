"""Stable database seam for in-process packages."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
import re

from sqlalchemy import MetaData, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import DeclarativeBase


def package_base(schema: str) -> type[DeclarativeBase]:
    """Create an independent declarative base owned by one package schema."""

    if not schema:
        raise ValueError("package schema must not be empty")

    class PackageBase(DeclarativeBase):
        metadata = MetaData(schema=schema)

    return PackageBase


@asynccontextmanager
async def package_session(schema: str) -> AsyncIterator[AsyncSession]:
    """Give a package a schema-local core transaction without exposing internals."""

    from services.backend.src.core.db import AsyncSessionLocal

    if schema == "public" or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", schema) is None:
        raise ValueError("package schema must be a non-public PostgreSQL identifier")
    async with AsyncSessionLocal() as session:
        try:
            await session.execute(text(f'SET LOCAL search_path TO "{schema}", public'))
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
