"""API Keys router."""

import json

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models import APIKey

from ..database import get_async_session
from ..schemas import APIKeyCreate, APIKeyRead

router = APIRouter(prefix="/api-keys", tags=["api-keys"])


@router.post("/", response_model=APIKeyRead, status_code=status.HTTP_201_CREATED)
async def create_api_key(
    key_in: APIKeyCreate,
    db: AsyncSession = Depends(get_async_session),
) -> APIKey:
    """Create or update an API key."""
    # Check if key exists for service/project
    query = select(APIKey).where(
        APIKey.service == key_in.service, APIKey.project_id == key_in.project_id
    )
    result = await db.execute(query)
    existing_key = result.scalar_one_or_none()

    # Serialize value if it's a dict
    if isinstance(key_in.value, dict):
        key_value = json.dumps(key_in.value)
    else:
        key_value = str(key_in.value)

    # TODO: Add real encryption here
    encrypted_value = key_value

    if existing_key:
        existing_key.key_enc = encrypted_value
        existing_key.type = key_in.type
        await db.commit()
        await db.refresh(existing_key)
        return existing_key

    new_key = APIKey(
        service=key_in.service,
        key_enc=encrypted_value,
        type=key_in.type,
        project_id=key_in.project_id,
    )
    db.add(new_key)
    await db.commit()
    await db.refresh(new_key)
    return new_key


@router.get("/{service}", response_model=dict)
async def get_api_key(
    service: str,
    project_id: str | None = None,
    db: AsyncSession = Depends(get_async_session),
) -> dict:
    """Get decoded API key value."""
    query = select(APIKey).where(APIKey.service == service, APIKey.project_id == project_id)
    result = await db.execute(query)
    api_key = result.scalar_one_or_none()

    if not api_key:
        raise HTTPException(status_code=404, detail="API Key not found")

    # TODO: Add real decryption here
    decrypted_value = api_key.key_enc

    try:
        return {"value": json.loads(decrypted_value)}
    except json.JSONDecodeError:
        return {"value": decrypted_value}
