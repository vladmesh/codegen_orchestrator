"""Per-user engineering-budget policy and ledger-derived balance API."""

from http import HTTPStatus

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.contracts.dto.engineering_budget_policy import (
    EngineeringBudgetPolicyCommand,
    EngineeringBudgetPolicyState,
)
from shared.models import EngineeringAttemptLedger, EngineeringBudgetPolicy, User

from ..database import get_async_session
from ..dependencies import get_current_user, require_internal_or_admin
from ..schemas.engineering_budget_policy import (
    EngineeringBudgetBalanceRead,
    EngineeringBudgetPolicyLookup,
    EngineeringBudgetPolicyRead,
)

router = APIRouter(prefix="/engineering-budget-policies", tags=["engineering-budget-policies"])
self_router = APIRouter(prefix="/engineering-budget-policy", tags=["engineering-budget-policies"])


def _policy_read(policy: EngineeringBudgetPolicy) -> EngineeringBudgetPolicyRead:
    return EngineeringBudgetPolicyRead.model_validate(policy)


def _enforcement(policy: EngineeringBudgetPolicy | None) -> str:
    if policy is None:
        return "unlimited"
    if policy.state is EngineeringBudgetPolicyState.DISABLED:
        return "not_enforced"
    return "enforced"


async def _get_policy(user_id: int, db: AsyncSession) -> EngineeringBudgetPolicy | None:
    return await db.get(EngineeringBudgetPolicy, user_id)


async def _policy_lookup(user_id: int, db: AsyncSession) -> EngineeringBudgetPolicyLookup:
    policy = await _get_policy(user_id, db)
    return EngineeringBudgetPolicyLookup(
        user_id=user_id,
        policy=_policy_read(policy) if policy is not None else None,
        enforcement=_enforcement(policy),
    )


async def _balance(user_id: int, db: AsyncSession) -> EngineeringBudgetBalanceRead:
    """Aggregate only immutable ledger facts attributed to this user."""
    policy = await _get_policy(user_id, db)
    known_spend, unknown_attempts = (
        await db.execute(
            select(
                func.coalesce(func.sum(EngineeringAttemptLedger.cost_microusd), 0),
                func.count().filter(EngineeringAttemptLedger.cost_microusd.is_(None)),
            ).where(EngineeringAttemptLedger.user_id == user_id)
        )
    ).one()
    known_spend_microusd = int(known_spend)
    unknown_cost_attempt_count = int(unknown_attempts)
    enforcement = _enforcement(policy)
    if enforcement != "enforced":
        remaining_microusd = None
        exhausted = False
    else:
        assert policy is not None
        remaining_microusd = max(policy.limit_microusd - known_spend_microusd, 0)
        exhausted = known_spend_microusd >= policy.limit_microusd
    return EngineeringBudgetBalanceRead(
        user_id=user_id,
        policy=_policy_read(policy) if policy is not None else None,
        enforcement=enforcement,
        known_spend_microusd=known_spend_microusd,
        remaining_microusd=remaining_microusd,
        exhausted=exhausted,
        unknown_cost_attempt_count=unknown_cost_attempt_count,
        incomplete_coverage=unknown_cost_attempt_count > 0,
    )


async def _require_existing_user(user_id: int, db: AsyncSession) -> None:
    if await db.get(User, user_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")


@router.put("/{user_id}", response_model=EngineeringBudgetPolicyRead)
async def put_engineering_budget_policy(
    user_id: int,
    command: EngineeringBudgetPolicyCommand,
    response: Response,
    db: AsyncSession = Depends(get_async_session),
    _: None = Depends(require_internal_or_admin),
) -> EngineeringBudgetPolicy:
    """Create or exactly update a policy with optimistic versioning.

    A retry that already describes the stored state succeeds without needing a
    version.  A different state must name the row's current version, which is
    the reservation seam for a later dispatch/admission increment.
    """
    await _require_existing_user(user_id, db)
    policy = await db.scalar(
        select(EngineeringBudgetPolicy)
        .where(EngineeringBudgetPolicy.user_id == user_id)
        .with_for_update()
    )
    if policy is None:
        if command.version is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Policy does not exist at the requested version",
            )
        policy = EngineeringBudgetPolicy(
            user_id=user_id,
            limit_microusd=command.limit_microusd,
            state=command.state,
            version=1,
        )
        db.add(policy)
        await db.commit()
        await db.refresh(policy)
        response.status_code = HTTPStatus.CREATED
        return policy

    if policy.limit_microusd == command.limit_microusd and policy.state is command.state:
        return policy
    if command.version != policy.version:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Policy version is stale or missing",
        )
    policy.limit_microusd = command.limit_microusd
    policy.state = command.state
    policy.version += 1
    await db.commit()
    await db.refresh(policy)
    return policy


@router.get("/{user_id}", response_model=EngineeringBudgetPolicyLookup)
async def get_named_engineering_budget_policy(
    user_id: int,
    db: AsyncSession = Depends(get_async_session),
    _: None = Depends(require_internal_or_admin),
) -> EngineeringBudgetPolicyLookup:
    """Read a named user's policy, for internal services and administrators."""
    await _require_existing_user(user_id, db)
    return await _policy_lookup(user_id, db)


@router.get("/{user_id}/balance", response_model=EngineeringBudgetBalanceRead)
async def get_named_engineering_budget_balance(
    user_id: int,
    db: AsyncSession = Depends(get_async_session),
    _: None = Depends(require_internal_or_admin),
) -> EngineeringBudgetBalanceRead:
    """Read a named user's ledger-derived balance, never an aggregate counter."""
    await _require_existing_user(user_id, db)
    return await _balance(user_id, db)


@self_router.get("", response_model=EngineeringBudgetPolicyLookup)
async def get_own_engineering_budget_policy(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_async_session),
) -> EngineeringBudgetPolicyLookup:
    """Read the authenticated user's own policy without exposing an identifier."""
    return await _policy_lookup(user.id, db)


@self_router.get("/balance", response_model=EngineeringBudgetBalanceRead)
async def get_own_engineering_budget_balance(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_async_session),
) -> EngineeringBudgetBalanceRead:
    """Read the authenticated user's own ledger-derived balance."""
    return await _balance(user.id, db)
